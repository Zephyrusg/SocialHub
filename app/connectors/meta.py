"""Meta Graph v26 adapters. All public mutations are disabled by default."""
import asyncio
import hashlib
import hmac
import re
import httpx
from app.core import HubError
from app.connectors.meta_policy import PAGE_ID, INSTAGRAM_ID


class GraphClient:
    def __init__(self, token: str, version: str, host='graph.facebook.com', transport=None, *, app_secret='', allow_writes=False):
        if host not in {'graph.facebook.com', 'graph.instagram.com'} or not re.fullmatch(r'v\d+\.\d+', version):
            raise ValueError('Invalid Graph API host or version')
        self.token, self.base_url, self.transport = token, f'https://{host}/{version}/', transport
        self.app_secret, self.allow_writes = app_secret, allow_writes

    async def get(self, path, params=None):
        return await self.request('GET', path, params)

    async def mutate(self, method, path, data):
        if not self.allow_writes:
            raise HubError('live_writes_disabled', 'Live writes are disabled for Phase 2A')
        if method not in {'POST', 'DELETE'}:
            raise HubError('unsupported_operation', 'Unsupported mutation method')
        return await self.request(method, path, data)

    async def request(self, method, path, values=None):
        if not re.fullmatch(r'[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)?', path):
            raise HubError('invalid_remote_id', 'Invalid Meta object identifier')
        values = dict(values or {})
        if self.app_secret:
            values['appsecret_proof'] = hmac.new(self.app_secret.encode(), self.token.encode(), hashlib.sha256).hexdigest()
        safe_retry = method == 'GET' and path != 'oauth/access_token'
        attempts = 3 if safe_retry else 1  # Never blindly retry public writes.
        # Meta may fetch/process a remote media URL before acknowledging the POST.
        # The old ten-second read limit could expire after the photo was published.
        media_post = method == 'POST' and path.rsplit('/', 1)[-1] in {'photos', 'videos'}
        timeout = httpx.Timeout(120 if media_post else 10, connect=10,
            write=30 if media_post else 10, pool=10)
        async with httpx.AsyncClient(base_url=self.base_url, transport=self.transport, timeout=timeout, follow_redirects=False) as client:
            for attempt in range(attempts):
                try:
                    response = await client.request(method, path, params=values if method == 'GET' else None,
                        data=values if method != 'GET' else None, headers={'Authorization': f'Bearer {self.token}'} if self.token else {})
                except (httpx.TimeoutException, httpx.RequestError) as error:
                    if attempt < attempts - 1:
                        await asyncio.sleep(0.05 * 2 ** attempt)
                        continue
                    timed_out = isinstance(error, httpx.TimeoutException)
                    diagnostics = {'mutation_attempted': method != 'GET'}
                    if timed_out:
                        diagnostics['timeout_phase'] = next((phase for kind, phase in [
                            (httpx.ConnectTimeout, 'connect'), (httpx.ReadTimeout, 'read'),
                            (httpx.WriteTimeout, 'write'), (httpx.PoolTimeout, 'pool')]
                            if isinstance(error, kind)), 'unknown')
                    raise HubError('api_timeout' if timed_out else 'api_unavailable',
                        ('Meta request timed out; retry the read' if safe_retry else 'Meta code exchange failed; start a new local Facebook Login' if path == 'oauth/access_token' else 'Write outcome uncertain; reconcile before any retry'), safe_retry, diagnostics=diagnostics) from None
                try:
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get('error', {}), dict):
                        raise ValueError()
                except ValueError:
                    raise HubError('invalid_api_response', 'Meta returned an invalid response', safe_retry, diagnostics={'mutation_attempted': method != 'GET'}) from None
                error = payload.get('error', {})
                code = error.get('code')
                limited = response.status_code == 429 or code in {4, 17, 32, 613, 80001, 80002}
                transient = limited or response.status_code >= 500 or error.get('is_transient', False)
                if (response.is_error or error) and transient and attempt < attempts - 1:
                    try:
                        delay = min(max(float(response.headers.get('Retry-After', '0.1')), 0), 2)
                    except ValueError:
                        delay = 0.1
                    await asyncio.sleep(delay * 2 ** attempt)
                    continue
                if code == 190:
                    raise HubError('token_expired', 'Meta token expired or revoked; reconnect with the local Facebook Login procedure')
                if response.is_error or error or response.is_redirect:
                    errcode = 'rate_limited' if limited else 'api_error'
                    message = 'Meta rate limit reached; wait before retrying' if limited else 'Meta request failed; check granted permissions, Page tasks and object access'
                    if code in {10, 200, 299}:
                        errcode, message = 'api_error', 'Meta denied permission; reconnect and verify the required scopes and Page tasks'
                    diagnostics = {}
                    if isinstance(code, int):
                        diagnostics['provider_code'] = code
                    if isinstance(error.get('error_subcode'), int):
                        diagnostics['provider_subcode'] = error['error_subcode']
                    if code == 100:
                        errcode, message = 'invalid_meta_object', 'Meta rejected the object or fields; verify the account and content identifier'
                        # Recognize a fixed provider error grammar; never retain the raw message,
                        # which can include a token, authorization code or request URI.
                        match = re.fullmatch(r'Tried accessing nonexisting field \(([a-z_]+)\) on node type \(([A-Za-z]+)\)', str(error.get('message', '')))
                        if match and match[1] in {'account_type', 'access_token', 'instagram_business_account', 'tasks', 'username', 'id'} and match[2] in {'IGUser', 'Page', 'User'}:
                            diagnostics.update(reason='unsupported_field', field=match[1], node_type=match[2])
                    raise HubError(errcode, message, transient and safe_retry, diagnostics=diagnostics)
                return payload


class MetaConnector:
    platform = ''
    post_fields = ''
    posts_edge = ''

    def __init__(self, graph: GraphClient, bound_account=None):
        self.graph, self.bound_account = graph, bound_account

    def check_account(self, remote_id):
        if self.bound_account and remote_id != self.bound_account:
            raise HubError('account_denied', 'Connector is bound to a different account')

    def capabilities(self, account_type):
        # Legacy fixture capability surface retained for Phase 1 mocks.
        return {'list_posts': True, 'get_post': True, 'list_comments': True,
                'publish_post': True, 'update_post': self.platform == 'facebook',
                'reply_to_comment': True, 'schedule_post': True,
                'analytics': False, 'stories': self.platform == 'instagram' and account_type == 'business',
                'live_writes_enabled': False}

    async def list_posts(self, remote_id, limit=25, cursor=None):
        self.check_account(remote_id)
        params = {'fields': self.post_fields, 'limit': limit}
        if cursor:
            params['after'] = cursor
        return self.page(await self.graph.get(f'{remote_id}/{self.posts_edge}', params))

    async def get_post(self, remote_id, post_id):
        self.check_account(remote_id)
        cursor, seen = None, set()
        for _ in range(100):
            page = await self.list_posts(remote_id, 100, cursor)
            for item in page['items']:
                if item.get('id') == post_id:
                    return item
            cursor = page.get('next_cursor')
            if not cursor:
                raise HubError('post_not_found', 'Post not found in the authorized account')
            if cursor in seen:
                raise HubError('invalid_api_response', 'Meta returned a repeated pagination cursor')
            seen.add(cursor)
        raise HubError('lookup_limit', 'Account ownership lookup exceeded 100 pages; use the account post listing to narrow the request')

    async def list_comments(self, remote_id, post_id, limit=25, cursor=None):
        await self.get_post(remote_id, post_id)
        params = {'fields': 'id,text,timestamp' if self.platform == 'instagram' else 'id,message,created_time', 'limit': limit}
        if cursor:
            params['after'] = cursor
        return self.page(await self.graph.get(f'{post_id}/comments', params))

    async def check_comment(self, remote_id, post_id, comment_id):
        cursor, seen = None, set()
        for _ in range(100):
            page = await self.list_comments(remote_id, post_id, 100, cursor)
            if any(c.get('id') == comment_id for c in page['items']):
                return
            cursor = page.get('next_cursor')
            if not cursor:
                raise HubError('comment_not_found', 'Comment does not belong to the authorized post')
            if cursor in seen:
                raise HubError('invalid_api_response', 'Meta returned a repeated comment cursor')
            seen.add(cursor)
        raise HubError('lookup_limit', 'Comment ownership lookup exceeded 100 pages')

    def write_guard(self, remote_id):
        self.check_account(remote_id)
        expected = PAGE_ID if self.platform == 'facebook' else INSTAGRAM_ID
        if remote_id != expected or self.bound_account != expected:
            raise HubError('account_denied', 'Live mutations require the explicitly authorized bound account')
        if not self.graph.allow_writes:
            raise HubError('live_writes_disabled', 'Live writes are disabled for Phase 2A')

    @staticmethod
    def page(payload):
        data = payload.get('data', [])
        paging = payload.get('paging') or {}
        if not isinstance(data, list) or not isinstance(paging, dict) or not all(isinstance(v, dict) for v in data):
            raise HubError('invalid_api_response', 'Meta returned invalid pagination data')
        # Ignore upstream next URLs (which may contain tokens); rebuild with a cursor.
        cursors = paging.get('cursors') or {}
        if not isinstance(cursors, dict):
            raise HubError('invalid_api_response', 'Meta returned invalid paging cursors')
        cursor = cursors.get('after') if paging.get('next') else None
        if cursor is not None and not isinstance(cursor, str):
            raise HubError('invalid_api_response', 'Meta returned an invalid cursor')
        if paging.get('next') and not cursor:
            raise HubError('invalid_api_response', 'Meta pagination has no safe continuation cursor')
        return {'items': data, 'next_cursor': cursor}


class FacebookConnector(MetaConnector):
    platform = 'facebook'
    attachment_fields = 'description,title,type,url,media,target'
    post_fields = ('id,message,story,created_time,permalink_url,full_picture,'
                   f'attachments{{{attachment_fields},subattachments{{{attachment_fields}}}}}')
    posts_edge = 'posts'

    @staticmethod
    def post_content(item):
        post = dict(item)
        # Missing message is normal for media/system posts. Never invent a
        # caption from a generated story or attachment title.
        for field in ('message', 'story', 'full_picture'):
            post.setdefault(field, None)
        post['attachments'] = FacebookConnector.attachment_content(post.get('attachments'))
        return post

    @staticmethod
    def attachment_content(connection, depth=0):
        if connection is None:
            return {'data': []}
        if (not isinstance(connection, dict) or not isinstance(connection.get('data', []), list)
                or depth > 1 or any(not isinstance(a, dict) for a in connection.get('data', []))):
            raise HubError('invalid_api_response', 'Meta returned invalid Facebook attachment data')
        items = []
        for item in connection.get('data', []):
            attachment = dict(item)
            if 'subattachments' in attachment:
                attachment['subattachments'] = FacebookConnector.attachment_content(attachment['subattachments'], depth + 1)
            items.append(attachment)
        result = {'data': items}
        # Attachment paging links can contain access tokens just like the
        # outer posts edge. Return an indicator, never those upstream URLs.
        if isinstance(connection.get('paging'), dict) and connection['paging'].get('next'):
            result['has_more'] = True
        return result

    async def list_posts(self, remote_id, limit=25, cursor=None):
        page = await super().list_posts(remote_id, limit, cursor)
        page['items'] = [self.post_content(item) for item in page['items']]
        return page

    async def discover(self, cursor=None):
        params = {'fields': 'id,name,tasks,instagram_business_account', 'limit': 100}
        if cursor:
            params['after'] = cursor
        return self.page(await self.graph.get('me/accounts', params))

    async def publish_post(self, remote_id, text):
        self.write_guard(remote_id)
        return await self.graph.mutate('POST', f'{remote_id}/feed', {'message': text})

    async def update_post(self, remote_id, post_id, text):
        self.write_guard(remote_id)
        await self.get_post(remote_id, post_id)
        # Text/message only; callers must not use this for unsupported content types.
        return await self.graph.mutate('POST', post_id, {'message': text})

    async def delete_post(self, remote_id, post_id):
        self.write_guard(remote_id)
        await self.get_post(remote_id, post_id)
        return await self.graph.mutate('DELETE', post_id, {})

    async def reply_to_comment(self, remote_id, post_id, comment_id, text):
        self.write_guard(remote_id)
        await self.check_comment(remote_id, post_id, comment_id)
        return await self.graph.mutate('POST', f'{comment_id}/comments', {'message': text})


class InstagramConnector(MetaConnector):
    platform = 'instagram'
    post_fields = 'id,caption,media_type,permalink,timestamp'
    posts_edge = 'media'

    async def discover(self):
        path = self.bound_account or 'me'
        return await self.graph.get(path, {'fields': 'id,username'})

    async def create_media_container(self, remote_id, media_url, caption, media_type='IMAGE'):
        self.write_guard(remote_id)
        url = httpx.URL(media_url)
        if url.scheme != 'https' or not url.host or url.username or url.password:
            raise HubError('invalid_media', 'Meta media ingestion requires an HTTPS media URL without credentials')
        if media_type not in {'IMAGE', 'REELS'}:
            raise HubError('unsupported_operation', 'Only image and Reel container methods are implemented')
        data = {'caption': caption, 'image_url' if media_type == 'IMAGE' else 'video_url': media_url}
        if media_type == 'REELS':
            data['media_type'] = 'REELS'
        return await self.graph.mutate('POST', f'{remote_id}/media', data)

    async def publish_media_container(self, remote_id, creation_id):
        self.write_guard(remote_id)
        # Creation ID must come from a stored request-bound operation in a future write service.
        raise HubError('authorization_required', 'Container publication requires a persisted account-bound creation record')

    async def publish_post(self, remote_id, media_url, caption, media_type='IMAGE'):
        self.write_guard(remote_id)
        container = await self.create_media_container(remote_id, media_url, caption, media_type)
        creation_id = container.get('id', '')
        status = await self.graph.get(creation_id, {'fields': 'status_code'})
        if status.get('status_code') != 'FINISHED':
            raise HubError('media_processing', 'Meta media is not ready; persist the container and poll its status before publication')
        # The ID originates in this account-scoped request, not caller input.
        return await self.graph.mutate('POST', f'{remote_id}/media_publish', {'creation_id': creation_id})

    async def update_post(self, remote_id, post_id, text):
        raise HubError('unsupported_operation', 'Instagram caption editing is unsupported; no delete/recreate fallback')

    async def reply_to_comment(self, remote_id, post_id, comment_id, text):
        self.write_guard(remote_id)
        await self.check_comment(remote_id, post_id, comment_id)
        return await self.graph.mutate('POST', f'{comment_id}/replies', {'message': text})
