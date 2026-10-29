"""Explicit, owned derivative creation. Conversion cannot prepare or publish."""
import asyncio
import hashlib
import hmac
import json
import secrets
import time
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from app.core import HubError
from app.connectors.reels import ReelWorkflows
from app.database.models import Audit, Idempotency, Media
from app.services.administration import EffectiveSettings
from app.services.media_budget import MediaBudget
from app.services.media_ingestion import validation_thread
from app.services.media_manager import MediaManager
from app.services.normalization_protocol import PROFILE, MAX_BYTES, WORKER_URL, request_signature, response_signature, valid_digest
from app.services.video_fingerprint import video_fingerprint


class MediaNormalization:
    def __init__(self, settings, sessions, transport=None):
        self.settings = settings if isinstance(settings, EffectiveSettings) else EffectiveSettings(settings, sessions)
        self.sessions, self.transport = sessions, transport
        self.manager = MediaManager(self.settings, sessions)
        self.budget = MediaBudget(self.settings, sessions)

    def enabled(self):
        if not self.settings.media_normalization_enabled or not self.settings.media_ingestion_enabled:
            raise HubError('media_normalization_disabled', 'Enable Instagram Media Normalization and Media Import in administrator settings first')

    def identities(self, principal, key, asset):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise HubError('invalid_request', 'A bounded normalization idempotency key is required')
        namespace = hashlib.sha256(('normalize:' + principal + ':' + key).encode()).hexdigest()
        request_hash = hashlib.sha256(json.dumps({'profile': PROFILE, 'source_id': asset['id'],
            'source_sha256': asset['sha256']}, sort_keys=True).encode()).hexdigest()
        return 'normalize:' + namespace, request_hash

    def cached(self, principal, key, request_hash):
        with self.sessions() as db:
            row = db.get(Idempotency, key)
            if row is None:return None
            if row.subject != principal or row.request_hash != request_hash:
                raise HubError('idempotency_conflict', 'Normalization key is bound to another source or profile')
            response = dict(row.response)
            asset = self.manager.get(response['media']['id'], principal)
            self.manager.path(asset['id'], asset['sha256'])
            return dict(response, media=asset, reused=True)

    async def transfer(self, source, source_path, destination):
        job = secrets.token_hex(16); expires = str(int(time.time()) + 90)
        secret = self.settings.media_normalization_secret.get_secret_value()
        size = source['size']; digest = source['sha256']
        async def chunks():
            with source_path.open('rb') as stream:
                while block := await asyncio.to_thread(stream.read, 65536):
                    yield block
        try:
            async with asyncio.timeout(240):
                async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                        timeout=httpx.Timeout(200, connect=5, pool=5), follow_redirects=False) as client:
                    async with client.stream('POST', WORKER_URL, content=chunks(), headers={
                            'Content-Type': 'application/octet-stream', 'Content-Length': str(size),
                            'X-Job': job, 'X-Expires': expires, 'X-Source-Sha256': digest,
                            'X-Signature': request_signature(secret, job, expires, digest, size)}) as response:
                        if response.status_code != 200:
                            raise ValueError()
                        length = int(response.headers['Content-Length'])
                        checksum = response.headers['X-Output-Sha256']
                        if (not 0 < length <= min(MAX_BYTES, self.settings.media_max_bytes)
                                or response.headers.get('Content-Type') != 'video/mp4'
                                or response.headers.get('Content-Encoding', 'identity') != 'identity'
                                or not valid_digest(checksum)
                                or not hmac.compare_digest(response_signature(secret, job, checksum, length), response.headers['X-Signature'])):
                            raise ValueError()
                        total = 0; actual = hashlib.sha256()
                        with destination.open('wb') as stream:
                            async for block in response.aiter_raw():
                                total += len(block)
                                if total > length:raise ValueError()
                                stream.write(block); actual.update(block)
                        if total != length or actual.hexdigest() != checksum:
                            raise ValueError()
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise HubError('media_normalization_unavailable', 'Private conversion did not return an authenticated result. The original is unchanged; retry the same key later or check worker readiness', True) from None

    def commit(self, path, principal, source, key, request_hash, fingerprint):
        mime, meta = self.manager.inspect(path)
        ReelWorkflows.validate_reel(SimpleNamespace(platform='instagram'), {'mime_type': mime, 'metadata': meta})
        original = source['metadata']
        if (any(meta[k] != original[k] for k in ['width', 'height', 'video_codec', 'frame_rate'])
                or abs(meta['duration'] - original['duration']) > .25
                or video_fingerprint(path) != fingerprint):
            raise HubError('invalid_media', 'Conversion changed the video stream or timeline unexpectedly; original preserved')
        # Recheck runtime switches after encoding and before registering a derivative.
        self.enabled()
        asset = self.manager.save(path, principal, consume=True)
        annotation = {'profile': PROFILE, 'video_stream_unchanged': True, 'audio_target_bps': 120000}
        with self.sessions.begin() as db:
            item = db.get(Media, asset['id'])
            item.metadata_json = dict(item.metadata_json, normalization=annotation)
            asset = self.manager.describe(item)
            response = {'media': asset, 'source_media_id': source['id'], 'profile': PROFILE,
                'reused': False, 'normalized': True, 'published': False,
                'expires_at': item.expires_at.isoformat() + 'Z' if item.expires_at else None,
                'review_required': 'New derivative: AAC audio may be re-encoded and MP4 timing may shift slightly. Review it and prepare a new exact publication before any publish instruction.'}
            db.add(Idempotency(key=key, subject=principal, request_hash=request_hash, response=response))
            db.add(Audit(subject=principal, operation='normalize_media', outcome='success', details={
                'source_media_id': source['id'], 'source_sha256': source['sha256'],
                'media_id': asset['id'], 'sha256': asset['sha256'], 'profile': PROFILE}))
        return response

    async def normalize(self, media_id, principal, idempotency_key):
        self.enabled()
        source = await asyncio.to_thread(self.manager.get, media_id, principal)
        if source['mime_type'] != 'video/mp4':
            raise HubError('invalid_media', 'Normalization requires an existing owned H.264/AAC MP4 asset')
        key, request_hash = self.identities(principal, idempotency_key, source)
        cached = await asyncio.to_thread(self.cached, principal, key, request_hash)
        if cached is not None:return cached
        with self.budget.upload() as path:
            cached = await asyncio.to_thread(self.cached, principal, key, request_hash)
            if cached is not None:return cached
            source_path, _ = await asyncio.to_thread(self.manager.path, source['id'], source['sha256'])
            try:
                ReelWorkflows.validate_reel(SimpleNamespace(platform='instagram'), source)
            except HubError:
                pass
            else:
                # Already valid: do not degrade audio or create needless storage.
                response = {'media': source, 'source_media_id': source['id'], 'profile': PROFILE,
                            'reused': True, 'normalized': False, 'already_compatible': True, 'published': False}
                self.enabled()
                with self.sessions.begin() as db:
                    db.add(Idempotency(key=key, subject=principal, request_hash=request_hash, response=response))
                    db.add(Audit(subject=principal, operation='normalize_media', outcome='already_compatible',
                        details={'media_id': source['id'], 'sha256': source['sha256'], 'profile': PROFILE}))
                return response
            fingerprint = await validation_thread(video_fingerprint, source_path)
            self.enabled()
            await self.transfer(source, source_path, path)
            return await validation_thread(self.commit, path, principal, source, key, request_hash, fingerprint)
