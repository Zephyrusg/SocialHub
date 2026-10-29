"""Platform-specific publishing workflows; orchestration stays platform-neutral."""
from typing import Protocol
from urllib.parse import urlparse, urlsplit, urlunsplit
import httpx
from app.core import HubError
from app.connectors.reels import ReelWorkflows, REEL_READ_STAGES


def facebook_permalink(value):
    """Meta can return a relative Reel URL; expose only a full Facebook link."""
    if (not isinstance(value, str) or not value or len(value) > 4096
            or '\\' in value or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        return None
    if value.startswith('/') and not value.startswith('//'):
        value = 'https://www.facebook.com' + value
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {'https', 'http'} or parts.hostname not in
                {'facebook.com', 'www.facebook.com', 'm.facebook.com', 'web.facebook.com'}
                or parts.username is not None or parts.password is not None or parts.port not in {None, 443}):
            return None
        return urlunsplit(('https', 'www.facebook.com', parts.path or '/', parts.query, ''))
    except ValueError:
        return None


class PublishingConnector(Protocol):
    async def validate(self, account_id, request, assets): ...
    async def step(self, account_id, request, checkpoint): ...


class MetaPublishingConnector(ReelWorkflows):
    def __init__(self, connector):
        self.connector = connector
        self.platform = connector.platform
        self.graph = connector.graph

    def permalink(self, info):
        value = info.get('permalink') or info.get('permalink_url') or info.get('link')
        return facebook_permalink(value) if self.platform == 'facebook' else value

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
                if request['content_type']=='video' and meta.get('duration',0)>600:
                    raise HubError('invalid_media','The existing Meta regular-video subset is limited to ten minutes; longer-file policy is for YouTube')
                if request['content_type'] == 'reel':
                    if len(request['text']) > 2200:
                        raise HubError('invalid_content', 'Reel captions are limited to 2200 characters in this service')
                    self.validate_reel(asset)
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

    async def finalize_delivery(self, account_id, request, checkpoint):
        """Resolve acknowledged delivery metadata; never calls a mutation method."""
        self.connector.check_account(account_id)
        state = dict(checkpoint)
        if state.get('stage') != 'delivered' or not state.get('remote_id'):
            raise HubError('reconciliation_denied', 'Only an acknowledged delivery can be finalized')
        if request['operation'] != 'publish':
            return {'state': 'succeeded', 'checkpoint': state, 'result': {'remote_id': state['remote_id'], 'permalink': state.get('permalink')}}
        fields = 'id,permalink' if self.platform == 'instagram' else ('id,link' if state.get('lookup_kind') == 'photo' else 'id,permalink_url')
        info = await self.read(state['remote_id'], {'fields': fields})
        if request['content_type'] == 'reel' and not self.permalink(info):
            return {'state': 'processing', 'checkpoint': state, 'retry_after': 60}
        return {'state': 'succeeded', 'checkpoint': state, 'result': {'remote_id': state['remote_id'], 'permalink': self.permalink(info)}}

    async def refresh_delivery(self, account_id, request, checkpoint):
        self.connector.check_account(account_id)
        if checkpoint.get('stage') == 'delivered':
            return await self.finalize_delivery(account_id, request, checkpoint)
        if request['content_type'] == 'reel':
            return await self.reel_refresh(account_id, request, checkpoint)
        if (self.platform != 'facebook' or request['operation'] != 'publish'
                or request['content_type'] != 'video' or checkpoint.get('stage') != 'video_processing'):
            raise HubError('unsupported_operation', 'Only acknowledged Facebook video uploads or deliveries can be polled read-only')
        state = dict(checkpoint)
        info = await self.read(state['remote_id'], {'fields': 'id,status,permalink_url'})
        status = (info.get('status') or {}).get('video_status')
        if status == 'error':
            raise HubError('media_processing_failed', 'Meta video processing failed; inspect this remote object instead of uploading a replacement')
        if status != 'ready':
            state['polls'] = min(int(state.get('polls', 0)) + 1, 60)
            return {'state': 'processing', 'checkpoint': state, 'retry_after': 60 if state['polls'] < 60 else 300}
        return {'state': 'succeeded', 'checkpoint': state,
                'result': {'remote_id': state['remote_id'], 'permalink': self.permalink(info)}}

    async def step(self, account_id, request, checkpoint):
        if checkpoint.get('stage') == 'delivered':
            return await self.finalize_delivery(account_id, request, checkpoint)
        if checkpoint.get('stage') == 'video_processing' and request['content_type'] == 'video':
            return await self.refresh_delivery(account_id, request, checkpoint)
        self.connector.write_guard(account_id)
        operation, kind, text = request['operation'], request['content_type'], request['text']
        state = dict(checkpoint)
        if operation == 'update':
            response = await self.graph.mutate('POST', request['post_id'], {'message': text})
            if response.get('success') is not True:
                raise HubError('write_outcome_unknown', 'Meta did not acknowledge the edit; reconcile before retrying')
            return self.delivered(request['post_id'])
        if operation == 'reply':
            edge = 'comments' if self.platform == 'facebook' else 'replies'
            response = await self.graph.mutate('POST', f"{request['comment_id']}/{edge}", {'message' if self.platform == 'facebook' else 'message': text})
            return self.delivered(self.remote_id(response))
        if kind == 'reel' and state.get('stage') != 'ig_container':
            return await self.reel_step(account_id, request, state)
        if self.platform == 'instagram':
            return await self.instagram(account_id, request, state)
        if state.get('stage') == 'video_processing':
            info = await self.read(state['remote_id'], {'fields': 'id,status,permalink_url'})
            status = (info.get('status') or {}).get('video_status')
            if status == 'error':
                raise HubError('media_processing_failed', 'Meta video processing failed')
            if status != 'ready':
                return {'state': 'processing', 'checkpoint': state, 'retry_after': 60}
            return {'state': 'succeeded', 'checkpoint': state, 'result': {'remote_id': state['remote_id'], 'permalink': self.permalink(info)}}
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

    @staticmethod
    def remote_id(response):
        value = str(response.get('post_id') or response.get('id') or '')
        if not value or not all(c.isalnum() or c == '_' for c in value):
            raise HubError('write_outcome_unknown', 'Meta did not return a valid remote identifier; reconcile before retrying')
        return value

    @staticmethod
    def delivered(remote_id):
        return {'state': 'processing', 'checkpoint': {'stage': 'delivered', 'remote_id': remote_id}}
