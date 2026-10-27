"""Facebook content uses synthetic Graph responses; no live social writes."""
import copy
import json
import httpx
import pytest
from pydantic import SecretStr
from app.connectors.meta import FacebookConnector, GraphClient
from app.connectors.meta_policy import PAGE_ID
from app.core import HubError
from app.database import database
from app.database.models import Base
from app.main import create_server
from app.services.meta_oauth import MetaOAuth
from test_meta_live import MetaFixture, settings

POST_ID = PAGE_ID + '_101'
BASE = {'id': POST_ID, 'created_time': '2026-10-01T10:00:00+0000',
        'permalink_url': 'https://www.facebook.com/example/posts/101'}
IMAGE = {'image': {'src': 'https://media.example.test/photo.jpg', 'width': 1200, 'height': 630}}


@pytest.mark.parametrize('content', [
    {'message': 'Actual text 🎵\nSecond line.'},
    {'message': 'Photo caption', 'full_picture': 'https://media.example.test/photo.jpg',
     'attachments': {'data': [{'type': 'photo', 'description': 'Photo caption', 'media': IMAGE,
                               'target': {'id': 'photo-1', 'url': 'https://www.facebook.com/example/photo'}}]}},
    {'story': 'PixelPapa updated the cover photo.', 'full_picture': 'https://media.example.test/cover.jpg',
     'attachments': {'data': [{'type': 'cover_photo', 'title': 'Cover photo', 'media': IMAGE}]}},
    {'message': 'Album caption', 'attachments': {'data': [{'type': 'album', 'subattachments': {'data': [
        {'type': 'photo', 'media': IMAGE}, {'type': 'video', 'media': IMAGE, 'description': 'Clip'}]}}]}},
    {}, {'message': '', 'attachments': None}, {'message': None, 'attachments': {'data': []}},
])
async def test_facebook_list_and_owned_detail_preserve_same_content(content):
    raw = BASE | copy.deepcopy(content)
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == 'GET'
        assert request.url.path == '/v26.0/' + PAGE_ID + '/posts'
        assert request.url.params['fields'] == FacebookConnector.post_fields
        for field in ['message', 'story', 'attachments', 'media', 'subattachments', 'full_picture']:
            assert field in request.url.params['fields']
        return httpx.Response(200, json={'data': [raw]})
    connector = FacebookConnector(GraphClient('synthetic-page-token', 'v26.0',
        transport=httpx.MockTransport(handler)), PAGE_ID)
    listed = await connector.list_posts(PAGE_ID)
    detail = await connector.get_post(PAGE_ID, POST_ID)
    assert detail == listed['items'][0]
    assert detail['message'] == content.get('message')
    assert detail['story'] == content.get('story')
    assert detail['full_picture'] == content.get('full_picture')
    assert detail['attachments'] == (content.get('attachments') or {'data': []})
    assert all(detail[k] == v for k, v in BASE.items())
    assert len(requests) == 2 and raw == BASE | content


async def test_attachment_paging_urls_never_escape_at_either_level():
    marker = 'never-return-paging-credential'
    paging = {'next': 'https://graph.facebook.com/private?access_token=' + marker}
    raw = BASE | {'attachments': {'data': [{'type': 'album', 'subattachments': {
        'data': [{'type': 'photo', 'media': IMAGE}], 'paging': paging}}], 'paging': paging}}
    connector = FacebookConnector(GraphClient('synthetic', 'v26.0', transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'data': [raw]}))), PAGE_ID)
    page = await connector.list_posts(PAGE_ID)
    detail = await connector.get_post(PAGE_ID, POST_ID)
    assert marker not in json.dumps(page) and marker not in json.dumps(detail)
    assert detail['attachments']['has_more']
    assert detail['attachments']['data'][0]['subattachments']['has_more']
    assert detail['attachments']['data'][0]['subattachments']['data'][0]['media'] == IMAGE


async def test_content_lookup_keeps_ownership_and_cursor_boundaries():
    seen = []
    def handler(request):
        seen.append(request)
        assert request.url.path == '/v26.0/' + PAGE_ID + '/posts'
        assert request.method == 'GET'
        if 'after' not in request.url.params:
            return httpx.Response(200, json={'data': [], 'paging': {'next': 'https://unsafe.invalid/?access_token=ignored',
                'cursors': {'after': 'owned-page-2'}}})
        assert request.url.params['after'] == 'owned-page-2'
        return httpx.Response(200, json={'data': [BASE | {'message': 'Owned text'}]})
    connector = FacebookConnector(GraphClient('synthetic', 'v26.0', transport=httpx.MockTransport(handler)), PAGE_ID)
    assert (await connector.get_post(PAGE_ID, POST_ID))['message'] == 'Owned text'
    with pytest.raises(HubError) as error:await connector.get_post(PAGE_ID, 'foreign-post')
    assert error.value.code == 'post_not_found'
    count = len(seen)
    with pytest.raises(HubError):await connector.list_posts('foreign-page')
    assert len(seen) == count


@pytest.mark.parametrize('attachments', ['invalid', {'data': None}, {'data': ['invalid']},
    {'data': [{'subattachments': {'data': [{'subattachments': {'data': []}}]}}]}])
async def test_malformed_or_excessive_attachment_nesting_rejected(attachments):
    connector = FacebookConnector(GraphClient('synthetic', 'v26.0', transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'data': [BASE | {'attachments': attachments}]}))), PAGE_ID)
    with pytest.raises(HubError) as error:await connector.list_posts(PAGE_ID)
    assert error.value.code == 'invalid_api_response'


async def test_facebook_content_reaches_mcp_text_and_structured_results(settings, monkeypatch, tmp_path):
    import app.services.hub as module
    url = f'sqlite:///{tmp_path}/facebook-mcp.db'
    _, factory = database(url);Base.metadata.create_all(factory.kw['bind'])
    cfg = settings.model_copy(update={'database_url': SecretStr(url)})
    fixture = MetaFixture()
    await MetaOAuth(cfg, factory, httpx.MockTransport(fixture)).connect('synthetic-user')
    raw = BASE | {'message': 'Readable Facebook text 🎵', 'attachments': {'data': [{'type': 'photo', 'media': IMAGE}]}}
    def handler(request):
        if request.url.path == '/v26.0/' + PAGE_ID + '/posts':
            assert request.method == 'GET'
            return httpx.Response(200, json={'data': [raw]})
        return fixture(request)
    original = module.GraphClient
    monkeypatch.setattr(module, 'GraphClient', lambda *a, **kw: original(*a, **kw, transport=httpx.MockTransport(handler)))
    app = create_server(cfg).streamable_http_app()
    headers = {'Authorization': 'Bearer ' + 'a' * 64, 'Accept': 'application/json, text/event-stream'}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8787', headers=headers) as client:
            await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'content-test', 'version': '1'}}})
            data = []
            for i, (name, args) in enumerate([('list_posts', {'account_id': 'pixelpapa-facebook'}),
                ('get_post', {'account_id': 'pixelpapa-facebook', 'post_id': POST_ID})], 2):
                response = await client.post('/mcp', json={'jsonrpc': '2.0', 'id': i, 'method': 'tools/call',
                    'params': {'name': name, 'arguments': args}})
                assert response.status_code == 200
                result = json.loads(next(s[6:] for s in response.text.splitlines() if s.startswith('data: ')))['result']
                structured = result['structuredContent']
                assert structured['ok'] and json.loads(result['content'][0]['text']) == structured
                data.append(structured['data'])
            assert data[0]['items'][0] == data[1]
            assert data[1]['message'] == raw['message'] and data[1]['attachments'] == raw['attachments']
