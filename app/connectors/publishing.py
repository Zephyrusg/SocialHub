"""Platform-specific publishing workflows; orchestration stays platform-neutral."""
from typing import Protocol
from urllib.parse import urlparse
import httpx
from app.core import HubError


class PublishingConnector(Protocol):
    async def validate(self, account_id, request, assets): ...
    async def step(self, account_id, request, checkpoint): ...


class MetaPublishingConnector:
    def __init__(self, connector):
        self.connector = connector
        self.platform = connector.platform
        self.graph = connector.graph

    async def validate(self, account_id, request, assets):
        self.connector.check_account(account_id)
        if self.platform == 'instagram':
            if request['operation'] == 'update' or (request['operation'] == 'publish' and request['content_type'] not in {'image', 'reel'}):
                raise HubError('unsupported_operation', 'Instagram supports image/Reel publication and replies; caption replacement is unsupported')
            if len(request['text']) > 2200:
                raise HubError('invalid_content', 'Instagram captions/replies are limited to 2200 characters in this service')
        if request['content_type'] == 'link':
            u = urlparse(request.get('link_url') or '')
            if u.scheme not in {'http', 'https'} or not u.netloc or u.username or u.password:
                raise HubError('invalid_content', 'Links require an HTTP(S) URL without embedded credentials')
        if assets:
            asset = assets[0]
            meta = asset['metadata']
            if request['content_type'] == 'image':
                if not asset['mime_type'].startswith('image/'):
                    raise HubError('invalid_media', 'Image content requires an image asset')
                if self.platform == 'facebook' and asset['size'] > 4 * 1024 * 1024:
                    raise HubError('invalid_media', 'Supported Facebook image subset is limited to 4 MiB')
                if self.platform == 'instagram':
                    ratio = meta['width'] / meta['height']
                    if asset['mime_type'] != 'image/jpeg' or asset['size'] > 8 * 1024 * 1024 or not 0.8 <= ratio <= 1.91 or not 320 <= meta['width'] <= 1080:
                        raise HubError('invalid_media', 'Instagram images require JPEG up to 8 MiB, width 320–1080, and aspect ratio 4:5 through 1.91:1')
            elif request['content_type'] in {'video', 'reel'}:
                if asset['mime_type'] != 'video/mp4':
                    raise HubError('invalid_media', 'Video/Reel content requires a validated MP4 asset')
                if request['content_type'] == 'reel' and (not 3 <= meta['duration'] <= 60 or abs(meta['width'] / meta['height'] - 9 / 16) > 0.02 or not 720 <= meta['width'] <= 1080):
                    raise HubError('invalid_media', 'Supported Reels subset: H.264/AAC MP4, 9:16, width 720–1080, 3–60 seconds')
        if request['operation'] != 'publish':
            post = await self.connector.get_post(account_id, request['post_id'])
            if request['operation'] == 'update':
                # Never assume photos/video/Reels can be rewritten as a generic feed post.
                details = await self.read(request['post_id'], {'fields': 'id,message,attachments{type,media_type}'})
                attachment_types = {str(a.get('type', a.get('media_type', 'unknown'))).lower() for a in (details.get('attachments') or {}).get('data', [])}
                if attachment_types - {'share', 'link', 'article'}:
                    raise HubError('unsupported_operation', 'Only text/link Page message edits are supported; media replacement is refused')
            else:
                await self.connector.check_comment(account_id, request['post_id'], request['comment_id'])

    async def read(self, path, params):
        try:
            return await self.graph.get(path, params)
        except HubError as error:
            error.diagnostics = dict(error.diagnostics, mutation_attempted=False)
            raise

    async def step(self, account_id, request, checkpoint):
        self.connector.write_guard(account_id)
        operation, kind, text = request['operation'], request['content_type'], request['text']
        state = dict(checkpoint)
        if state.get('stage') == 'delivered':
            if operation != 'publish':
                return {'state': 'succeeded', 'checkpoint': state, 'result': {'remote_id': state['remote_id'], 'permalink': state.get('permalink')}}
            fields = 'id,permalink' if self.platform == 'instagram' else ('id,link' if state.get('lookup_kind') == 'photo' else 'id,permalink_url')
            info = await self.read(state['remote_id'], {'fields': fields})
            return {'state': 'succeeded', 'checkpoint': state, 'result': {'remote_id': state['remote_id'], 'permalink': info.get('permalink') or info.get('permalink_url') or info.get('link')}}
        if operation == 'update':
            response = await self.graph.mutate('POST', request['post_id'], {'message': text})
            if response.get('success') is not True:
                raise HubError('write_outcome_unknown', 'Meta did not acknowledge the edit; reconcile before retrying')
            return self.delivered(request['post_id'])
        if operation == 'reply':
            edge = 'comments' if self.platform == 'facebook' else 'replies'
            response = await self.graph.mutate('POST', f"{request['comment_id']}/{edge}", {'message' if self.platform == 'facebook' else 'message': text})
            return self.delivered(self.remote_id(response))
        if self.platform == 'instagram':
            return await self.instagram(account_id, request, state)
        if kind == 'reel':
            return await self.facebook_reel(account_id, request, state)
        if state.get('stage') == 'video_processing':
            info = await self.read(state['remote_id'], {'fields': 'id,status,permalink_url'})
            status = (info.get('status') or {}).get('video_status')
            if status == 'error':
                raise HubError('media_processing_failed', 'Meta video processing failed')
            if status != 'ready':
                return {'state': 'processing', 'checkpoint': state, 'retry_after': 60}
            return {'state': 'succeeded', 'checkpoint': state, 'result': {'remote_id': state['remote_id'], 'permalink': info.get('permalink_url')}}
        if kind in {'text', 'link'}:
            data = {'message': text}
            if kind == 'link':
                data['link'] = request['link_url']
            response = await self.graph.mutate('POST', f'{account_id}/feed', data)
        elif kind == 'image':
            response = await self.graph.mutate('POST', f'{account_id}/photos', {'url': request['media_url'], 'message': text, 'published': 'true'})
        elif kind == 'video':
            response = await self.graph.mutate('POST', f'{account_id}/videos', {'file_url': request['media_url'], 'description': text, 'published': 'true'})
            return {'state': 'processing', 'checkpoint': {'stage': 'video_processing', 'remote_id': self.remote_id(response)}, 'retry_after': 60}
        else:
            raise HubError('unsupported_operation', 'Unsupported Facebook publication type')
        result = self.delivered(self.remote_id(response))
        if kind == 'image' and not response.get('post_id'):
            result['checkpoint']['lookup_kind'] = 'photo'
        return result

    async def instagram(self, account_id, request, state):
        if not state:
            response = await self.connector.create_media_container(account_id, request['media_url'], request['text'], 'REELS' if request['content_type'] == 'reel' else 'IMAGE')
            return {'state': 'processing', 'checkpoint': {'stage': 'ig_container', 'container_id': self.remote_id(response)}, 'retry_after': 60}
        info = await self.read(state['container_id'], {'fields': 'status_code'})
        status = info.get('status_code')
        if status in {'ERROR', 'EXPIRED'}:
            raise HubError('media_processing_failed', 'Instagram container failed or expired; no replacement was silently created')
        if status == 'PUBLISHED':
            raise HubError('write_outcome_unknown', 'Container is published without a recorded media ID; reconcile instead of republishing')
        if status != 'FINISHED':
            return {'state': 'processing', 'checkpoint': state, 'retry_after': 60}
        response = await self.graph.mutate('POST', f'{account_id}/media_publish', {'creation_id': state['container_id']})
        return self.delivered(self.remote_id(response))

    async def facebook_reel(self, account_id, request, state):
        stage = state.get('stage')
        if not stage:
            response = await self.graph.mutate('POST', f'{account_id}/video_reels', {'upload_phase': 'start'})
            video_id = str(response.get('video_id', ''))
            upload_url = response.get('upload_url', '')
            u = urlparse(upload_url)
            if not video_id.isdigit() or u.scheme != 'https' or u.hostname != 'rupload.facebook.com' or u.port not in {None, 443} or u.username or u.password or u.query or u.fragment or not u.path.endswith('/' + video_id):
                raise HubError('write_outcome_unknown', 'Meta returned an invalid upload session; reconcile before retrying')
            return {'state': 'processing', 'checkpoint': {'stage': 'fb_reel_upload', 'remote_id': video_id, 'upload_url': upload_url}}
        if stage == 'fb_reel_upload':
            await self.upload_reel(state['upload_url'], request['media_url'])
            return {'state': 'processing', 'checkpoint': {'stage': 'fb_reel_finish', 'remote_id': state['remote_id']}}
        if stage == 'fb_reel_finish':
            response = await self.graph.mutate('POST', f'{account_id}/video_reels', {'upload_phase': 'finish', 'video_id': state['remote_id'], 'video_state': 'PUBLISHED', 'description': request['text']})
            if response.get('success') is not True:
                raise HubError('write_outcome_unknown', 'Meta did not acknowledge Reel publication')
            return {'state': 'processing', 'checkpoint': {'stage': 'video_processing', 'remote_id': state['remote_id']}, 'retry_after': 60}
        info = await self.read(state['remote_id'], {'fields': 'id,status,permalink_url'})
        status = info.get('status') or {}
        if status.get('video_status') == 'error' or (status.get('publishing_phase') or {}).get('status') == 'error':
            raise HubError('media_processing_failed', 'Meta Reel processing failed')
        if (status.get('publishing_phase') or {}).get('status') != 'complete':
            return {'state': 'processing', 'checkpoint': state, 'retry_after': 60}
        return {'state': 'succeeded', 'checkpoint': state, 'result': {'remote_id': state['remote_id'], 'permalink': info.get('permalink_url')}}

    async def upload_reel(self, upload_url, media_url):
        # Never fetch an arbitrary upload URL or retry an ambiguous upload.
        u = urlparse(upload_url)
        if u.scheme != 'https' or u.hostname != 'rupload.facebook.com' or u.username or u.password or u.query or u.fragment or u.port not in {None, 443} or not u.path.startswith('/video-upload/'):
            raise HubError('invalid_meta_upload', 'Refused untrusted Meta upload endpoint')
        try:
            async with httpx.AsyncClient(transport=self.graph.transport, timeout=30, follow_redirects=False) as client:
                response = await client.post(upload_url, headers={'Authorization': 'OAuth ' + self.graph.token, 'file_url': media_url})
                if response.status_code != 200 or response.json().get('success') is not True:
                    raise HubError('write_outcome_unknown', 'Reel upload not acknowledged; reconcile before retrying')
        except (httpx.RequestError, ValueError):
            raise HubError('write_outcome_unknown', 'Reel upload outcome uncertain; reconcile before retrying') from None

    @staticmethod
    def remote_id(response):
        value = str(response.get('post_id') or response.get('id') or '')
        if not value or not all(c.isalnum() or c == '_' for c in value):
            raise HubError('write_outcome_unknown', 'Meta did not return a valid remote identifier; reconcile before retrying')
        return value

    @staticmethod
    def delivered(remote_id):
        return {'state': 'processing', 'checkpoint': {'stage': 'delivered', 'remote_id': remote_id}}
