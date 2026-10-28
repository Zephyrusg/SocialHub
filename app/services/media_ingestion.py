"""Authenticated attachment ingestion, stable idempotency and ephemeral probes."""
import asyncio
import hashlib
import json
from sqlalchemy import select
from app.core import HubError
from app.database.models import Idempotency, Audit, Media
from app.services.media_manager import MediaManager
from app.services.media_budget import MediaBudget
from app.services.attachment_download import AttachmentDownloader, attachment_host


async def validation_thread(fn, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Keep the lock and staging file until the bounded validator finishes.
        try: await task
        except Exception: pass
        raise


class MediaIngestion:
    def __init__(self, settings, sessions, downloader=None):
        self.settings, self.sessions = settings, sessions
        self.manager = MediaManager(settings, sessions)
        self.budget = MediaBudget(settings, sessions)
        self.downloader = downloader or AttachmentDownloader(settings)

    def identities(self, principal, key, reference):
        if not 1 <= len(key) <= 128:
            raise HubError('invalid_request', 'A bounded media import idempotency key is required')
        namespace = hashlib.sha256(('media-import:' + principal + ':' + key).encode()).hexdigest()
        digest = hashlib.sha256(json.dumps({'operation': 'import_media', 'file_id': reference.file_id}, sort_keys=True).encode()).hexdigest()
        return 'media:' + namespace, digest

    def cached(self, principal, key, digest):
        with self.sessions() as db:
            row = db.get(Idempotency, key)
            if row is None: return None
            if row.subject != principal or row.request_hash != digest:
                raise HubError('idempotency_conflict', 'Media import key is bound to a different attachment')
            asset = self.manager.get(row.response['media']['id'], principal)
            self.manager.path(asset['id'], asset['sha256'])
            return self.result(asset, reused=True)

    def result(self, asset, reused=False):
        with self.sessions() as db:
            item = db.get(Media, asset['id'])
            expires = item.expires_at.isoformat() + 'Z' if item.expires_at else None
        return {'media': asset, 'expires_at': expires, 'reused': reused,
                'suggested_content_type': 'image' if asset['mime_type'].startswith('image/') else 'video',
                'published': False}

    def commit_import(self, path, principal, key, digest):
        asset = self.manager.save(path, principal, consume=True)
        with self.sessions.begin() as db:
            db.add(Idempotency(key=key, subject=principal, request_hash=digest, response={'media': asset}))
            db.add(Audit(subject=principal, operation='import_media', outcome='success',
                details={'media_id': asset['id'], 'sha256': asset['sha256'], 'size': asset['size']}))
        return self.result(asset)

    async def import_file(self, reference, principal, idempotency_key):
        if not self.settings.media_ingestion_enabled:
            raise HubError('media_ingestion_disabled', 'Attachment ingestion is not activated for this environment')
        key, digest = self.identities(principal, idempotency_key, reference)
        cached = await asyncio.to_thread(self.cached, principal, key, digest)
        if cached is not None: return cached
        with self.budget.upload() as path:
            with path.open('wb') as output:
                await self.downloader.receive(reference, output)
            return await validation_thread(self.commit_import, path, principal, key, digest)

    async def probe(self, reference):
        host = attachment_host(reference)
        observation = {'file_parameters_received': True, 'download_host': host,
                       'download_verified': False, 'stored': False, 'published': False}
        if not self.settings.media_attachment_probe_enabled or host not in self.settings.media_download_hosts:
            return dict(observation, mode='reference_only',
                probe_enabled=self.settings.media_attachment_probe_enabled,
                exact_host_allowed=host in self.settings.media_download_hosts)
        with self.budget.upload() as path:
            with path.open('wb') as output:
                size = await self.downloader.receive(reference, output)
            mime, metadata = await validation_thread(self.manager.inspect, path)
            digest = await asyncio.to_thread(self.manager.file_hash, path)
            return dict(observation, mode='validated_temporary_transfer', download_verified=True,
                        mime_type=mime, size=size, sha256=digest, metadata=metadata)
