import httpx
import pytest
from app.connectors.meta import GraphClient, FacebookConnector, InstagramConnector
from app.core import HubError


@pytest.mark.parametrize('cls,edge', [(FacebookConnector, 'posts'), (InstagramConnector, 'media')])
async def test_meta_read_and_cursor(cls, edge):
    seen = []
    def handler(request):
        seen.append(request)
        assert request.headers['Authorization'] == 'Bearer secret'
        assert 'access_token' not in request.url.params
        return httpx.Response(200, json={'data': [{'id': 'p1'}], 'paging': {'next': 'https://untrusted.invalid/token', 'cursors': {'after': 'next-cursor'}}})
    connector = cls(GraphClient('secret', 'v25.0', transport=httpx.MockTransport(handler)))
    result = await connector.list_posts('123', 10, 'cursor')
    assert seen[0].url.path == f'/v25.0/123/{edge}'
    assert seen[0].url.params['after'] == 'cursor'
    item = {'id': 'p1'}
    if cls is FacebookConnector:
        item.update(message=None, story=None, full_picture=None, attachments={'data': []})
    assert result == {'items': [item], 'next_cursor': 'next-cursor'}
    assert 'untrusted' not in str(result)


async def test_discovery():
    graph = GraphClient('secret', 'v25.0', transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'data': [{'id': '123', 'name': 'PixelPapa'}]})))
    assert (await FacebookConnector(graph).discover())['items'][0]['name'] == 'PixelPapa'


async def test_retry_transient():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={'error': {'code': 4}}, headers={'Retry-After': '0'}) if len(calls) < 3 else httpx.Response(200, json={'data': []})
    await GraphClient('x', 'v25.0', transport=httpx.MockTransport(handler)).get('123/posts')
    assert len(calls) == 3


@pytest.mark.parametrize('status,payload,code', [(400, {'error': {'code': 190, 'message': 'secret'}}, 'token_expired'), (429, {'error': {'code': 4}}, 'rate_limited'), (403, {'error': {'code': 10}}, 'api_error')])
async def test_safe_api_errors(status, payload, code):
    graph = GraphClient('secret', 'v25.0', transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload, headers={'Retry-After': '0'})))
    with pytest.raises(HubError) as error:
        await graph.get('123/posts')
    assert error.value.code == code
    assert 'secret' not in str(error.value)


async def test_timeout():
    def handler(request):
        raise httpx.ReadTimeout('secret-token')
    with pytest.raises(HubError) as error:
        await GraphClient('secret', 'v25.0', transport=httpx.MockTransport(handler)).get('123/posts')
    assert error.value.code == 'api_timeout' and error.value.retryable
    assert 'secret' not in str(error.value)


async def test_invalid_response():
    graph = GraphClient('x', 'v25.0', transport=httpx.MockTransport(lambda r: httpx.Response(200, text='<html>')))
    with pytest.raises(HubError) as error:
        await graph.get('123/posts')
    assert error.value.code == 'invalid_api_response'


async def test_object_ownership_denies_foreign_post():
    graph = GraphClient('x', 'v25.0', transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'data': [{'id': 'own'}]})))
    with pytest.raises(HubError) as error:
        await InstagramConnector(graph).list_comments('123', 'foreign')
    assert error.value.code == 'post_not_found'


async def test_url_injection_blocked():
    graph = GraphClient('x', 'v25.0')
    with pytest.raises(HubError):
        await graph.get('https://attacker.invalid')
