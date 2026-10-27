from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs
import httpx
import pytest
from sqlalchemy import select, func
from cryptography.fernet import Fernet
from app.config import Settings
from app.core import HubError
from app.database.models import Account, Credential, OAuthFlow, Capability
from app.connectors.meta import GraphClient, FacebookConnector, InstagramConnector
from app.connectors.meta_policy import PAGE_ID, INSTAGRAM_ID, REQUESTED_SCOPES, capabilities
from app.security.tokens import TokenVault
from app.services.meta_oauth import MetaOAuth
from app.services.hub import Hub


@pytest.fixture
def settings():
    return Settings(_env_file=None, database_url='sqlite://', encryption_key=Fernet.generate_key().decode(), local_token='a'*64,
                    meta_app_id='12345', meta_app_secret='fake-app-secret', meta_live_reads=True)


class MetaFixture:
    def __init__(self):
        self.calls = []
        self.scopes = list(REQUESTED_SCOPES)
        self.tasks = ['ANALYZE', 'CREATE_CONTENT', 'MODERATE']
        self.page_id, self.ig_id = PAGE_ID, INSTAGRAM_ID
        self.valid, self.app_id, self.kind = True, '12345', 'MEDIA_CREATOR'
        self.expires = int((datetime.now(timezone.utc) + timedelta(days=45)).timestamp())

    def __call__(self, request):
        self.calls.append(request)
        assert request.method == 'GET', 'OAuth/onboarding/read verification must not publish'
        path = request.url.path.removeprefix('/v26.0/')
        bearer = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if path == 'oauth/access_token':
            return httpx.Response(200, json={'access_token': 'long-user' if 'fb_exchange_token' in request.url.params else 'short-user', 'expires_in': 5000000})
        if path == 'debug_token':
            page = request.url.params['input_token'] == 'page-token'
            return httpx.Response(200, json={'data': {'is_valid': self.valid, 'app_id': self.app_id, 'type': 'PAGE' if page else 'USER', 'scopes': self.scopes, 'expires_at': 0 if page else self.expires}})
        if path == 'me/permissions':
            return httpx.Response(200, json={'data': [{'permission': p, 'status': 'granted'} for p in self.scopes]})
        if path == 'me/accounts':
            if 'after' not in request.url.params:
                return httpx.Response(200, json={'data': [{'id': 'unrelated', 'name': 'Private unrelated Page'}], 'paging': {'next': 'https://untrusted.invalid/?access_token=secret', 'cursors': {'after': 'second'}}})
            return httpx.Response(200, json={'data': [{'id': self.page_id, 'name': 'PixelPapa', 'tasks': self.tasks, 'instagram_business_account': {'id': self.ig_id}}]})
        if path == PAGE_ID:
            return httpx.Response(200, json={'id': self.page_id, 'access_token': 'page-token'})
        if path == 'me' and bearer == 'page-token':
            return httpx.Response(200, json={'id': self.page_id, 'name': 'PixelPapa', 'instagram_business_account': {'id': self.ig_id}})
        if path == INSTAGRAM_ID:
            assert request.url.params['fields'] == 'id,username', 'Facebook Login IGUser must not request account_type'
            payload = {'id': self.ig_id, 'username': 'pixelpapamusic'}
            if self.kind != 'MEDIA_CREATOR':
                payload['account_type'] = self.kind
            return httpx.Response(200, json=payload)
        if path in {f'{PAGE_ID}/posts', f'{INSTAGRAM_ID}/media'}:
            return httpx.Response(200, json={'data': []})
        raise AssertionError('Unexpected fixture endpoint')


@pytest.fixture
def meta(sessions, settings):
    fixture = MetaFixture()
    oauth = MetaOAuth(settings, sessions, httpx.MockTransport(fixture))
    return oauth, fixture


async def test_oauth_exchange_connect_encryption_and_exact_allowlist(meta, sessions):
    oauth, fixture = meta
    link = oauth.issue_login()
    ticket = parse_qs(urlparse(link).query)['ticket'][0]
    url, browser = oauth.start(ticket)
    state = parse_qs(urlparse(url).query)['state'][0]
    result = await oauth.complete(state, browser, 'one-time-code')
    assert result['connected'] == [PAGE_ID, INSTAGRAM_ID]
    assert result['instagram_type'] == 'creator'
    with sessions() as db:
        accounts = list(db.scalars(select(Account)))
        assert {a.remote_id for a in accounts} == {PAGE_ID, INSTAGRAM_ID}
        for credential in db.scalars(select(Credential)):
            assert 'page-token' not in credential.ciphertext
            assert 'long-user' not in credential.ciphertext
            assert oauth.vault.decrypt(credential)['access_token'] == 'page-token'
            assert credential.expires_at is not None
        assert db.scalar(select(func.count()).select_from(Capability)) > 0
    assert len([c for c in fixture.calls if c.url.path.endswith('/oauth/access_token')]) == 2


def test_login_ticket_one_use_and_only_hashes(meta, sessions):
    oauth, _ = meta
    link = oauth.issue_login()
    ticket = parse_qs(urlparse(link).query)['ticket'][0]
    url, browser = oauth.start(ticket)
    state = parse_qs(urlparse(url).query)['state'][0]
    with sessions() as db:
        flow = db.scalar(select(OAuthFlow))
        assert ticket not in flow.ticket_hash and state not in flow.state_hash and browser not in flow.browser_hash
    with pytest.raises(HubError):
        oauth.start(ticket)
    oauth.consume(state, browser)
    with pytest.raises(HubError):
        oauth.consume(state, browser)


async def test_browser_binding_and_no_network_before_valid_state(meta):
    oauth, fixture = meta
    ticket = parse_qs(urlparse(oauth.issue_login()).query)['ticket'][0]
    url, browser = oauth.start(ticket)
    state = parse_qs(urlparse(url).query)['state'][0]
    with pytest.raises(HubError) as error:
        await oauth.complete(state, 'wrong-browser', 'code')
    assert error.value.code == 'invalid_oauth_state' and not fixture.calls
    with pytest.raises(HubError) as error:
        await oauth.complete(state, browser, None, denied=True)
    assert error.value.code == 'oauth_denied' and not fixture.calls
    with pytest.raises(HubError):
        oauth.consume(state, browser)


def test_expired_ticket(meta, sessions):
    oauth, _ = meta
    ticket = parse_qs(urlparse(oauth.issue_login()).query)['ticket'][0]
    with sessions.begin() as db:
        db.scalar(select(OAuthFlow)).expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    with pytest.raises(HubError):
        oauth.start(ticket)


@pytest.mark.parametrize('attribute,value,code', [('valid', False, 'invalid_meta_token'), ('app_id', 'other-app', 'invalid_meta_token'), ('ig_id', 'foreign-ig', 'account_mismatch'), ('page_id', 'foreign-page', 'account_denied'), ('scopes', [], 'permission_denied'), ('tasks', ['ADVERTISE'], 'permission_denied'), ('kind', 'PERSONAL', 'unsupported_account_type'), ('expires', 1, 'token_expired')])
async def test_validation_failure_is_atomic(meta, sessions, attribute, value, code):
    oauth, fixture = meta
    setattr(fixture, attribute, value)
    with pytest.raises(HubError) as error:
        await oauth.connect('long-user')
    assert error.value.code == code
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Credential)) == 0
        assert db.scalar(select(func.count()).select_from(Account)) == 0


async def test_live_reads_real_adapters_and_capabilities(meta, settings, sessions, monkeypatch):
    oauth, fixture = meta
    await oauth.connect('long-user')
    import app.services.hub as module
    original = module.GraphClient
    monkeypatch.setattr(module, 'GraphClient', lambda *a, **kw: original(*a, **kw, transport=httpx.MockTransport(fixture)))
    hub = Hub(sessions, settings)
    hub.seed_mocks()
    accounts = (await hub.read('list_accounts'))['accounts']
    assert len(accounts) == 2 and all(not a['mock'] for a in accounts)
    for aid in ['pixelpapa-facebook', 'pixelpapa-instagram']:
        assert (await hub.read('list_posts', aid, limit=25, cursor=None))['items'] == []
        caps = await hub.read('get_account_capabilities', aid)
        assert caps['capabilities']['list_posts']
        assert not caps['capabilities']['live_writes_enabled']
        with pytest.raises(HubError) as error:
            hub.write('publish_post', aid, {'text': 'no'}, 'key', 'test', ['socialhub:mock-write'])
        assert error.value.code == 'live_writes_disabled'
    with pytest.raises(HubError):
        await hub.read('list_posts', 'mock-facebook', limit=1, cursor=None)
    assert (await hub.read('get_system_status'))['mode'] == 'live_read_only'


async def test_arbitrary_live_account_never_exposed(meta, settings, sessions):
    oauth, _ = meta
    await oauth.connect('long-user')
    with sessions.begin() as db:
        db.add(Account(id='foreign', platform='facebook', remote_id='evil', name='Hidden', account_type='page', allowlisted=True, is_mock=False))
    hub = Hub(sessions, settings)
    assert len((await hub.read('list_accounts'))['accounts']) == 2
    with pytest.raises(HubError) as error:
        await hub.read('get_account_capabilities', 'foreign')
    assert error.value.code == 'account_denied'


async def test_revalidation_and_revocation(meta, sessions):
    oauth, fixture = meta
    await oauth.connect('long-user')
    assert (await oauth.revalidate())['connected'] == [PAGE_ID, INSTAGRAM_ID]
    with sessions.begin() as db:
        c = db.scalar(select(Credential).where(Credential.account_id == 'pixelpapa-facebook'))
        c.revoked = True
    with pytest.raises(HubError) as error:
        await oauth.revalidate()
    assert error.value.code == 'token_expired'


@pytest.mark.parametrize('platform', ['facebook', 'instagram'])
def test_write_caps_require_tasks_and_scopes(platform):
    caps = capabilities(platform, REQUESTED_SCOPES, ['ANALYZE'], 'creator')
    assert caps['list_posts'] and not caps['publish_post'] and not caps['reply_to_comment']
    assert not caps['schedule_post'] and not caps['live_writes_enabled']


def test_feature_flag_cannot_bypass_model_b(settings):
    with pytest.raises(ValueError, match='Phase 2A'):
        Settings(_env_file=None, database_url='sqlite://', encryption_key=settings.encryption_key, local_token='a'*64, meta_live_writes=True)


async def test_writes_disabled_before_any_http():
    def no_network(request):
        raise AssertionError('Disabled write performed HTTP')
    for cls, aid in [(FacebookConnector, PAGE_ID), (InstagramConnector, INSTAGRAM_ID)]:
        connector = cls(GraphClient('token', 'v26.0', transport=httpx.MockTransport(no_network)), aid)
        with pytest.raises(HubError) as error:
            if cls is FacebookConnector:
                await connector.publish_post(aid, 'text')
            else:
                await connector.publish_post(aid, 'https://example.com/a.jpg', 'caption')
        assert error.value.code == 'live_writes_disabled'


async def test_actual_write_payloads_only_against_mock_transport():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'id': 'remote-id'})
    graph = GraphClient('token', 'v26.0', transport=httpx.MockTransport(handler), allow_writes=True)
    assert (await FacebookConnector(graph, PAGE_ID).publish_post(PAGE_ID, 'hello'))['id'] == 'remote-id'
    assert seen[0].method == 'POST' and seen[0].url.path.endswith(f'/{PAGE_ID}/feed')
    assert b'message=hello' in seen[0].content
    await InstagramConnector(graph, INSTAGRAM_ID).create_media_container(INSTAGRAM_ID, 'https://example.com/a.jpg', 'caption')
    assert seen[-1].method == 'POST' and b'image_url=' in seen[-1].content


async def test_appsecret_proof_and_malformed_json():
    import hmac, hashlib
    def handler(request):
        assert request.url.params['appsecret_proof'] == hmac.new(b'app-secret', b'token', hashlib.sha256).hexdigest()
        return httpx.Response(200, json=[])
    with pytest.raises(HubError) as error:
        await GraphClient('token', 'v26.0', app_secret='app-secret', transport=httpx.MockTransport(handler)).get('me')
    assert error.value.code == 'invalid_api_response'


async def test_mutation_timeout_never_retried():
    seen = []
    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout('sensitive-upstream-details')
    with pytest.raises(HubError) as error:
        await GraphClient('token', 'v26.0', transport=httpx.MockTransport(handler), allow_writes=True).mutate('POST', f'{PAGE_ID}/feed', {'message': 'hello'})
    assert len(seen) == 1 and not error.value.retryable and 'uncertain' in error.value.message


async def test_repeated_cursor_and_missing_safe_cursor():
    with pytest.raises(HubError):
        FacebookConnector.page({'data': [], 'paging': {'next': 'https://evil.invalid/?token=x'}})
    def handler(request):
        return httpx.Response(200, json={'data': [], 'paging': {'next': 'https://evil.invalid', 'cursors': {'after': 'same'}}})
    with pytest.raises(HubError) as error:
        await FacebookConnector(GraphClient('x', 'v26.0', transport=httpx.MockTransport(handler))).get_post(PAGE_ID, 'unknown')
    assert error.value.code == 'invalid_api_response'


async def test_oauth_routes_cookie_security_and_redacted_errors(settings, sessions, monkeypatch, tmp_path):
    from app.main import create_server
    from app.database import database
    from app.database.models import Base
    url = f'sqlite:///{tmp_path}/routes.db'
    engine, factory = database(url)
    Base.metadata.create_all(engine)
    cfg = settings.model_copy(update={'database_url': __import__('pydantic').SecretStr(url)})
    oauth = MetaOAuth(cfg, factory)
    link = oauth.issue_login()
    server = create_server(cfg).streamable_http_app()
    async with server.router.lifespan_context(server):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='https://localhost:8788', headers={'X-Forwarded-Proto': 'https'}) as client:
            start = await client.get(link, follow_redirects=False)
            assert start.status_code == 303
            cookie = start.headers['set-cookie']
            assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=lax' in cookie
            assert start.headers['cache-control'] == 'no-store'
            state = parse_qs(urlparse(start.headers['location']).query)['state'][0]
            response = await client.get('/meta/oauth/callback', params={'state': state, 'error': 'access_denied', 'error_description': 'SHOULD_NOT_ECHO'})
            assert response.status_code == 400 and 'oauth_denied' in response.text
            assert 'SHOULD_NOT_ECHO' not in response.text
            response = await client.get('/meta/oauth/callback', params={'state': state, 'code': 'SECRET_CODE'})
            assert response.status_code == 400 and 'SECRET_CODE' not in response.text
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://localhost') as client:
            assert (await client.get('/meta/oauth/start?ticket=x')).status_code == 403


async def test_http_live_read_mcp_uses_real_connector(settings, sessions, monkeypatch, tmp_path):
    from app.main import create_server
    from app.database import database
    from app.database.models import Base
    import app.services.hub as module
    import json
    url = f'sqlite:///{tmp_path}/live-mcp.db'
    _, factory = database(url)
    Base.metadata.create_all(factory.kw['bind'])
    cfg = settings.model_copy(update={'database_url': __import__('pydantic').SecretStr(url)})
    fixture = MetaFixture()
    await MetaOAuth(cfg, factory, httpx.MockTransport(fixture)).connect('long-user')
    original = module.GraphClient
    monkeypatch.setattr(module, 'GraphClient', lambda *a, **kw: original(*a, **kw, transport=httpx.MockTransport(fixture)))
    app = create_server(cfg).streamable_http_app()
    headers = {'Authorization': 'Bearer ' + 'a'*64, 'Accept': 'application/json, text/event-stream'}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8787', headers=headers) as client:
            await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}})
            response = await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'list_posts', 'arguments': {'account_id': 'pixelpapa-instagram'}}})
            result = json.loads(next(s[6:] for s in response.text.splitlines() if s.startswith('data: ')))
            assert result['result']['structuredContent'] == {'ok': True, 'data': {'items': [], 'next_cursor': None}}


async def test_live_revocation_invalidates_both_credentials(meta, settings, sessions, monkeypatch):
    oauth, _ = meta
    await oauth.connect('long-user')
    import app.services.hub as module
    original = module.GraphClient
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={'error': {'code': 190}}))
    monkeypatch.setattr(module, 'GraphClient', lambda *a, **kw: original(*a, **kw, transport=transport))
    with pytest.raises(HubError) as error:
        await Hub(sessions, settings).read('list_posts', 'pixelpapa-instagram', limit=25, cursor=None)
    assert error.value.code == 'token_expired'
    with sessions() as db:
        assert all(c.revoked for c in db.scalars(select(Credential)))


async def test_facebook_edit_delete_reply_mock_http():
    seen = []
    def handler(request):
        seen.append(request)
        if request.method != 'GET':
            return httpx.Response(200, json={'success': True})
        if request.url.path.endswith('/posts'):
            return httpx.Response(200, json={'data': [{'id': 'own_post', 'message': 'text'}]})
        if request.url.path.endswith('/comments'):
            return httpx.Response(200, json={'data': [{'id': 'own_comment'}]})
        raise AssertionError('Unexpected request')
    connector = FacebookConnector(GraphClient('x', 'v26.0', transport=httpx.MockTransport(handler), allow_writes=True), PAGE_ID)
    await connector.update_post(PAGE_ID, 'own_post', 'edit')
    await connector.reply_to_comment(PAGE_ID, 'own_post', 'own_comment', 'reply')
    await connector.delete_post(PAGE_ID, 'own_post')
    assert [r.method for r in seen if r.method != 'GET'] == ['POST', 'POST', 'DELETE']
    with pytest.raises(HubError):
        await connector.update_post(PAGE_ID, 'foreign_post', 'bad')


async def test_instagram_publish_and_reply_mock_http():
    seen = []
    def handler(request):
        seen.append(request)
        path = request.url.path.split('/')[-1]
        if request.method == 'POST':
            return httpx.Response(200, json={'id': 'container' if path == 'media' else 'published'})
        if path == 'container':
            return httpx.Response(200, json={'status_code': 'FINISHED'})
        if path == 'media':
            return httpx.Response(200, json={'data': [{'id': 'owned_media'}]})
        if path == 'comments':
            return httpx.Response(200, json={'data': [{'id': 'owned_comment'}]})
        raise AssertionError('Unexpected request')
    connector = InstagramConnector(GraphClient('x', 'v26.0', transport=httpx.MockTransport(handler), allow_writes=True), INSTAGRAM_ID)
    assert (await connector.publish_post(INSTAGRAM_ID, 'https://example.com/img.jpg', 'caption'))['id'] == 'published'
    await connector.reply_to_comment(INSTAGRAM_ID, 'owned_media', 'owned_comment', 'reply')
    assert [r.url.path.split('/')[-1] for r in seen if r.method == 'POST'] == ['media', 'media_publish', 'replies']
    with pytest.raises(HubError) as error:
        await connector.update_post(INSTAGRAM_ID, 'owned_media', 'new caption')
    assert error.value.code == 'unsupported_operation'


async def test_failed_revalidation_revokes_existing_credentials(meta, sessions):
    oauth, fixture = meta
    await oauth.connect('long-user')
    fixture.valid = False
    with pytest.raises(HubError):
        await oauth.revalidate()
    with sessions() as db:
        assert all(c.revoked for c in db.scalars(select(Credential)))


def test_configuration_errors_do_not_echo_secrets():
    with pytest.raises(ValueError) as error:
        Settings(_env_file=None, database_url='mysql+pymysql://owner:database-private@localhost/socialhub',
                 encryption_key=Fernet.generate_key().decode(), local_token='a'*64,
                 meta_app_secret='app-private-value', meta_live_writes=True)
    assert 'database-private' not in str(error.value)
    assert 'app-private-value' not in str(error.value)


async def test_facebook_login_ig_fields_and_declared_subtype(meta):
    oauth, fixture = meta
    result = await oauth.connect('long-user')
    request = next(r for r in fixture.calls if r.url.path.endswith('/' + INSTAGRAM_ID))
    assert request.url.params['fields'] == 'id,username'
    assert result['instagram_type'] == 'creator'
    assert result['instagram_type_source'] == 'owner_declared'


@pytest.mark.parametrize('failed_stage,endpoint', [
    ('authorization_code_exchange', 'oauth/access_token'),
    ('user_token_validation', 'debug_token'),
    ('permission_discovery', 'me/permissions'),
    ('page_discovery', 'me/accounts'),
    ('page_token_retrieval', PAGE_ID),
    ('page_identity_verification', 'me'),
    ('instagram_account_discovery', INSTAGRAM_ID),
])
async def test_safe_oauth_failure_stage(meta, failed_stage, endpoint):
    oauth, fixture = meta
    def handler(request):
        path = request.url.path.removeprefix('/v26.0/')
        if path == endpoint:
            return httpx.Response(400, json={'error': {'code': 100, 'error_subcode': 33,
                'message': 'RAW_SECRET_AND_AUTHORIZATION_CODE_MUST_NOT_ESCAPE'}})
        return fixture(request)
    oauth.transport = httpx.MockTransport(handler)
    ticket = parse_qs(urlparse(oauth.issue_login()).query)['ticket'][0]
    url, browser = oauth.start(ticket)
    state = parse_qs(urlparse(url).query)['state'][0]
    with pytest.raises(HubError) as error:
        await oauth.complete(state, browser, 'synthetic-code')
    assert error.value.stage == failed_stage
    assert error.value.diagnostics == {'provider_code': 100, 'provider_subcode': 33}
    assert 'RAW_SECRET' not in error.value.message
    assert 'RAW_SECRET' not in str(error.value.diagnostics)


async def test_unsupported_field_diagnostics_contain_no_raw_message():
    def handler(request):
        return httpx.Response(400, json={'error': {'code': 100,
            'message': 'Tried accessing nonexisting field (account_type) on node type (IGUser)'}})
    with pytest.raises(HubError) as error:
        await GraphClient('synthetic', 'v26.0', transport=httpx.MockTransport(handler)).get(INSTAGRAM_ID)
    assert error.value.diagnostics == {'provider_code': 100, 'reason': 'unsupported_field', 'field': 'account_type', 'node_type': 'IGUser'}


def test_safe_failure_audit_and_cli_summary(sessions):
    from app.security.meta_routes import service_log
    from app.services.meta_diagnostics import latest_failure
    service_log(sessions, 'invalid_meta_object', 'instagram_account_discovery', {'provider_code': 100, 'reason': 'unsupported_field', 'field': 'account_type', 'node_type': 'IGUser'})
    result = latest_failure(sessions)
    assert result['stage'] == 'instagram_account_discovery' and result['provider_code'] == 100
    assert result['field'] == 'account_type'
