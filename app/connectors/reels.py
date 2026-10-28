"""Native Reel workflows. Each mutation is a separate durable orchestration step.

Status readers never publish. An uncertain upload or finish is never resent.
"""
import asyncio
import hashlib
import os
import stat
from pathlib import Path
import httpx
from app.core import HubError


REEL_READ_STAGES = frozenset({'fb_reel_processing', 'fb_reel_publishing', 'ig_reel_processing'})
REEL_UPLOAD_STAGES = frozenset({'fb_reel_upload', 'ig_reel_upload'})
REEL_READY_STAGES = frozenset({'fb_reel_ready', 'ig_reel_ready'})


class ReelWorkflows:
    def validate_reel(self, asset):
        m = asset['metadata']
        minimum = 4 if self.platform == 'facebook' else 3
        maximum = 60 if self.platform == 'facebook' else 600  # Service ingestion limit is ten minutes.
        if (asset['mime_type'] != 'video/mp4' or m.get('video_codec') != 'h264'
                or any(c != 'aac' for c in m.get('audio_codecs', []))
                or not minimum <= m['duration'] <= maximum
                or m['width'] * 16 != m['height'] * 9
                or not 720 <= m['width'] <= 1080
                or not 23 <= m.get('frame_rate', 0) <= 60
                or m.get('pixel_format') != 'yuv420p'
                or m.get('field_order') not in {'progressive', 'unknown'}
                or (self.platform == 'instagram' and (not m.get('fast_start') or m.get('has_edit_list', True)))
                or not 0 < m.get('video_bit_rate', 0) <= 25_000_000
                or len(m.get('audio_details', [])) != len(m.get('audio_codecs', []))
                or any(not 0 < a.get('sample_rate', 0) <= 48000 or a.get('channels') not in {1, 2}
                       or not 0 < a.get('bit_rate', 0)
                       or (self.platform == 'instagram' and a['bit_rate'] > 128000)
                       for a in m.get('audio_details', []))):
            extra = '; Instagram additionally requires fast-start without edit lists and AAC up to 128 kbps' if self.platform == 'instagram' else ''
            raise HubError('invalid_media', f'{self.platform.title()} Reel subset: MP4, progressive H.264/yuv420p, optional mono/stereo AAC up to 48 kHz, 9:16, width 720–1080, 23–60 fps, {minimum}–{maximum} seconds{extra}. Re-import older media if validation metadata is missing')

    def reel_upload_url(self, remote_id):
        if not isinstance(remote_id, str) or not remote_id.isascii() or not remote_id.isdigit():
            raise HubError('invalid_meta_upload', 'Invalid Reel upload identifier')
        version = self.graph.base_url.rstrip('/').rsplit('/', 1)[-1]
        edge = 'video-upload' if self.platform == 'facebook' else 'ig-api-upload'
        return f'https://rupload.facebook.com/{edge}/{version}/{remote_id}'

    async def upload_reel_file(self, remote_id, request):
        """One bounded binary POST, fixed host/path, no redirects, proxy env or retries."""
        if not self.graph.allow_writes:
            raise HubError('live_writes_disabled', 'Reel binary uploads require the live write guard', diagnostics={'mutation_attempted': False})
        url = self.reel_upload_url(remote_id)
        def open_checked():
            stream = os.fdopen(os.open(Path(request['_media_path']), os.O_RDONLY | os.O_NOFOLLOW), 'rb')
            try:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != request['_media_size']:
                    raise ValueError()
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                if digest != request['_media_sha256']:
                    raise ValueError()
                stream.seek(0)
                return stream
            except BaseException:
                stream.close()
                raise
        try:
            stream = open_checked()
        except (OSError, ValueError, KeyError):
            raise HubError('invalid_media', 'Private media changed or expired before upload', diagnostics={'mutation_attempted': False}) from None
        async def chunks():
            while block := await asyncio.to_thread(stream.read, 64 * 1024):
                yield block
        try:
            async with asyncio.timeout(150):
                async with httpx.AsyncClient(transport=self.graph.transport, trust_env=False,
                        timeout=httpx.Timeout(120, connect=10, pool=10), follow_redirects=False) as client:
                    response = await client.post(url, content=chunks(), headers={
                        'Authorization': 'OAuth ' + self.graph.token, 'offset': '0',
                        'file_size': str(request['_media_size']), 'Content-Length': str(request['_media_size']),
                        'Content-Type': 'application/octet-stream'})
                    acknowledgement = response.json() if response.status_code == 200 else None
                    if not isinstance(acknowledgement, dict) or acknowledgement.get('success') is not True:
                        raise ValueError()
        except (httpx.RequestError, TimeoutError, ValueError):
            raise HubError('write_outcome_unknown', 'Reel upload was not acknowledged; inspect the saved remote ID without resending', diagnostics={'mutation_attempted': True}) from None
        finally:
            stream.close()

    async def reel_step(self, account_id, request, checkpoint):
        state = dict(checkpoint)
        stage = state.get('stage')
        fb = self.platform == 'facebook'
        prefix = 'fb' if fb else 'ig'
        if len(request['text']) > 2200:
            raise HubError('invalid_content', 'Reel captions are limited to 2200 characters in this service', diagnostics={'mutation_attempted': False})
        if not state:
            try:
                response = await self.graph.mutate('POST', f'{account_id}/video_reels' if fb else f'{account_id}/media',
                    {'upload_phase': 'start'} if fb else {'media_type': 'REELS', 'upload_type': 'resumable', 'caption': request['text']})
            except HubError as error:
                if not fb and error.code == 'api_error' and error.diagnostics.get('provider_code') == 100:
                    error.message = 'Meta refused the Instagram resumable session; verify Facebook Login for Business eligibility, permissions and API support. No hosted-URL fallback or replacement was attempted'
                raise
            remote_id = str(response.get('video_id' if fb else 'id', ''))
            expected = self.reel_upload_url(remote_id)
            if response.get('upload_url' if fb else 'uri') != expected:
                raise HubError('write_outcome_unknown', 'Meta did not return the expected version-bound upload session; no upload attempted')
            return {'state': 'processing', 'checkpoint': {'stage': prefix + '_reel_upload', 'remote_id': remote_id,
                    **({} if fb else {'container_id': remote_id})}}
        if stage == prefix + '_reel_upload':
            await self.upload_reel_file(state['remote_id'], request)
            state['stage'] = prefix + '_reel_processing'
            return {'state': 'processing', 'checkpoint': state, 'retry_after': 60}
        if stage in REEL_READ_STAGES:
            return await self.reel_refresh(account_id, request, state)
        if stage == prefix + '_reel_ready':
            # Persisted readiness is rechecked immediately before the final mutation.
            ready = await self.reel_refresh(account_id, request, state)
            if ready['checkpoint'].get('stage') != stage:
                return ready
            response = await self.graph.mutate('POST', f'{account_id}/video_reels' if fb else f'{account_id}/media_publish',
                {'upload_phase': 'finish', 'video_id': state['remote_id'], 'video_state': 'PUBLISHED', 'description': request['text']}
                if fb else {'creation_id': state['container_id']})
            if not fb:
                delivered = self.delivered(self.remote_id(response))
                delivered['checkpoint']['container_id'] = state['container_id']
                return delivered
            if response.get('success') is not True:
                raise HubError('write_outcome_unknown', 'Meta did not acknowledge Reel finish; poll the existing video instead of finishing again')
            state['stage'] = 'fb_reel_publishing'
            return {'state': 'processing', 'checkpoint': state, 'retry_after': 60}
        raise HubError('reconciliation_denied', 'Unknown Reel checkpoint; no replacement was created')

    async def reel_refresh(self, account_id, request, checkpoint, *, uncertain=False):
        """GET only, including recovery of a known ID after ambiguous mutations."""
        self.connector.check_account(account_id)
        state = dict(checkpoint)
        stage = state['stage']
        if self.platform == 'facebook' and stage == 'video_processing':
            state['stage'] = stage = 'fb_reel_publishing'
        if self.platform == 'instagram':
            info = await self.read(state['container_id'], {'fields': 'status_code'})
            status = info.get('status_code')
            if status in {'ERROR', 'EXPIRED'}:
                raise HubError('media_processing_failed', 'Instagram Reel container failed or expired; no replacement created', diagnostics={'mutation_attempted': False})
            if status == 'PUBLISHED':
                raise HubError('write_outcome_unknown', 'Instagram reports PUBLISHED without a saved media ID; independently reconcile the permalink, never republish', diagnostics={'mutation_attempted': False})
            ready = status == 'FINISHED'
            if uncertain and stage == 'ig_reel_ready':
                ready = False  # media_publish might have succeeded; never repeat it.
            if ready:
                state['stage'] = 'ig_reel_ready'
        else:
            info = await self.read(state['remote_id'], {'fields': 'id,status,permalink_url'})
            status = info.get('status') or {}
            phases = [status.get(k) or {} for k in ('uploading_phase', 'processing_phase', 'publishing_phase')]
            if status.get('video_status') == 'error' or any(p.get('status') == 'error' for p in phases):
                raise HubError('media_processing_failed', 'Facebook Reel processing failed; no replacement created', diagnostics={'mutation_attempted': False})
            if phases[2].get('status') == 'complete':
                if not self.permalink(info):
                    return {'state': 'processing', 'checkpoint': dict(state, stage='delivered'), 'retry_after': 60}
                return {'state': 'succeeded', 'checkpoint': dict(state, stage='delivered'),
                        'result': {'remote_id': state['remote_id'], 'permalink': self.permalink(info)}}
            if uncertain and stage == 'fb_reel_ready':
                state['stage'] = 'fb_reel_publishing'  # A finish may have been accepted.
            elif stage != 'fb_reel_publishing' and phases[0].get('status') == 'complete' and phases[1].get('status') == 'complete':
                state['stage'] = 'fb_reel_ready'
        state['polls'] = min(int(state.get('polls', 0)) + 1, 60)
        return {'state': 'processing', 'checkpoint': state,
                'retry_after': 0 if state['stage'] in REEL_READY_STAGES else 60 if state['polls'] < 60 else 300,
                'uncertain': uncertain and (stage == 'ig_reel_ready' or state['stage'] not in REEL_READY_STAGES)}
