import json
import httpx
import pytest
from cryptography.fernet import Fernet
from app.config import Settings
from app.database import database
from app.database.models import Base
from app.main import create_server
from app.services.hub import Hub


@pytest.fixture
def server(tmp_path):
    url = f'sqlite:///{tmp_path}/mcp.db'
    engine, sessions = database(url)
    Base.metadata.create_all(engine)
    Hub(sessions).seed_mocks()
    settings = Settings(_env_file=None, database_url=url, encryption_key=Fernet.generate_key().decode(), local_token='a'*64)
    return create_server(settings)


def unpack(response):
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('text/event-stream')
    return json.loads(next(l[6:] for l in response.text.splitlines() if l.startswith('data: ')))


async def test_mcp_http_discovery_validation_and_writes(server):
    app = server.streamable_http_app()
    headers = {'Authorization': 'Bearer ' + 'a'*64, 'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost', headers=headers) as client:
            init = unpack(await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'pytest', 'version': '1'}}}))
            assert init['result']['serverInfo']['name'] == 'SocialHub'
            discovered = unpack(await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}))['result']['tools']
            assert {'list_accounts', 'list_posts', 'get_post', 'list_comments', 'publish_post', 'update_post', 'reply_to_comment', 'schedule_post'} <= {t['name'] for t in discovered}
            assert next(t for t in discovered if t['name'] == 'publish_post')['annotations']['readOnlyHint'] is False
            async def call(name, args):
                return unpack(await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}))['result']
            accounts = await call('list_accounts', {})
            assert 'structuredContent' in accounts, accounts
            assert len(accounts['structuredContent']['data']['accounts']) == 2
            bad = await call('list_posts', {'account_id': 'mock-facebook', 'limit': 101})
            assert bad['isError']
            denied = await call('publish_post', {'account_id': 'foreign', 'text': 'hello', 'idempotency_key': 'one'})
            assert denied['structuredContent']['error']['code'] == 'account_denied'
            args = {'account_id': 'mock-facebook', 'text': 'hello', 'idempotency_key': 'same'}
            first, second = await call('publish_post', args), await call('publish_post', args)
            assert first['structuredContent']['data']['id'] == second['structuredContent']['data']['id']


async def test_http_auth_and_resource_discovery(server):
    app = server.streamable_http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            response = await client.post('/mcp', json={})
            assert response.status_code == 401
            assert 'resource_metadata' in response.headers['www-authenticate']
            response = await client.get('/.well-known/oauth-protected-resource/mcp')
            assert response.status_code == 200
            assert response.json()['resource'] == 'http://localhost:8787/mcp'
            assert (await client.get('/healthz')).status_code == 200
            assert (await client.get('/metrics')).status_code == 401


async def test_dns_rebinding_blocked(server):
    app = server.streamable_http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://attacker.example', headers={'Authorization': 'Bearer ' + 'a'*64}) as client:
            assert (await client.post('/mcp', json={})).status_code == 421
