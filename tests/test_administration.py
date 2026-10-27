"""Administration security and setup use synthetic identities, temporary DBs and fake Meta."""
import json
import re
import time
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from starlette.datastructures import QueryParams
from app.core import HubError
from app.main import create_server
from app.database.models import AdminSession, ApprovalPortalSession, SiteConfiguration, Audit, Credential, OAuthFlow, utcnow
from app.services.administration import AdminOIDC, AdminSessions, Administration, EffectiveSettings, effective_settings, require_publisher
from app.services.meta_oauth import MetaOAuth
from app.services.hub import Hub
from test_production_auth import settings, ORIGIN
from test_meta_live import MetaFixture
from test_publishing import publisher


def config(sessions, **kw):
    return settings(**({'database_url': str(sessions.kw['bind'].url), 'admin_enabled': True,
        'admin_oidc_client_id': 'admin-client', 'admin_oidc_client_secret': 'synthetic-admin-client-secret',
        'admin_allowed_subjects': ['owner-sub', 'other-admin'], 'publishing_allowed_subjects': ['owner-sub'],
        'meta_app_id': '12345', 'meta_app_secret': 'fake-app-secret'} | kw))


@pytest.fixture
async def admin_oidc(sessions, monkeypatch):
    s = config(sessions)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state = {'change': {}, 'typ': 'JWT', 'requests': []}
    def transport(request):
        if request.url.path.endswith('openid-configuration'):
            return httpx.Response(200, json={'issuer': s.oauth_issuer, 'jwks_uri': s.oauth_jwks_url,
                'authorization_endpoint': s.oauth_issuer + '/authorize', 'token_endpoint': s.oauth_issuer + '/token',
                'code_challenge_methods_supported': ['S256'], 'token_endpoint_auth_methods_supported': ['client_secret_basic'],
                'authorization_response_iss_parameter_supported': True})
        state['requests'].append(request)
        claims = {'iss': s.oauth_issuer, 'aud': 'admin-client', 'sub': 'owner-sub', 'iat': int(time.time()),
            'exp': int(time.time()) + 60, 'auth_time': int(time.time()), 'nonce': state['nonce'], 'amr': ['pwd', 'otp']} | state['change']
        return httpx.Response(200, json={'id_token': jwt.encode(claims, key, algorithm='RS256', headers={'typ': state['typ']})})
    service = AdminOIDC(s, sessions, httpx.MockTransport(transport))
    monkeypatch.setattr(service.keys, 'get_signing_key_from_jwt', lambda _: SimpleNamespace(key=key.public_key()))
    url, browser = await service.start();query = parse_qs(urlsplit(url).query);state['nonce'] = query['nonce'][0]
    return service, state, query, browser


def callback(service, query, **changes):
    return QueryParams({'state': query['state'][0], 'code': 'synthetic-admin-code', 'iss': service.settings.oauth_issuer} | changes)


async def test_admin_oidc_mfa_pkce_session_and_no_approval_portal(admin_oidc, sessions):
    service, state, query, browser = admin_oidc
    assert query['redirect_uri'] == [ORIGIN + '/admin/oidc/callback']
    assert query['code_challenge_method'] == ['S256'] and query['max_age'] == ['900']
    token = await service.complete(callback(service, query), browser)
    assert service.portal.authenticate(token) == 'owner-sub'
    with sessions() as db:
        assert db.scalar(select(ApprovalPortalSession)) is None
        assert db.scalar(select(AdminSession)).token_hash != token
    assert state['requests'][0].headers['authorization'].startswith('Basic ')
    with pytest.raises(HubError):await service.complete(callback(service, query), browser)


@pytest.mark.parametrize('change', [
    {'sub': 'outsider'}, {'aud': 'mcp-client'}, {'nonce': 'forged'}, {'amr': ['pwd']},
    {'auth_time': 1}, {'auth_time': None}, {'auth_time': True}, {'auth_time': time.time() + 10000},
    {'azp': 'other-client'}, {'exp': 1}, {'iss': 'https://wrong.test'},
])
async def test_admin_oidc_claims_fail_closed(admin_oidc, sessions, change):
    service, state, query, browser = admin_oidc;state['change'] = change
    with pytest.raises((HubError, jwt.PyJWTError)):await service.complete(callback(service, query), browser)
    with sessions() as db:assert db.scalar(select(AdminSession)) is None


async def test_admin_oidc_cookie_and_client_purpose_binding(admin_oidc, sessions):
    from app.database.models import OwnerOIDCFlow
    service, state, query, browser = admin_oidc
    with pytest.raises(HubError):await service.complete(callback(service, query), 'wrong-cookie')
    with sessions.begin() as db:
        flow = db.scalar(select(OwnerOIDCFlow));data = json.loads(service.vault.cipher.decrypt(flow.ciphertext.encode()))
        data['purpose'] = 'approval';flow.ciphertext = service.vault.encrypt(data)
    with pytest.raises(HubError):await service.complete(callback(service, query), browser)
    with sessions() as db:assert db.scalar(select(AdminSession)) is None


async def test_admin_access_token_is_not_an_id_token(admin_oidc):
    service, state, query, browser = admin_oidc;state['typ'] = 'at+jwt'
    with pytest.raises(HubError):await service.complete(callback(service, query), browser)


async def test_admin_ui_is_independent_csrf_protected_and_redacts_secrets(sessions):
    s = config(sessions);backend = AdminSessions(s, sessions);token = backend.create_session('owner-sub')
    app = create_server(s).streamable_http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        assert (await c.get('/admin/', headers={'Authorization': 'Bearer any-mcp-token'})).status_code == 403
        c.cookies.set('__Secure-socialhub-admin', token)
        response = await c.get('/admin/');assert response.status_code == 200
        assert 'Publishing permission for this verified subject: yes' in response.text
        assert 'fake-app-secret' not in response.text and 'synthetic-admin-client-secret' not in response.text
        assert response.headers['referrer-policy'] == 'strict-origin'
        assert response.headers['cache-control'] == 'no-store' and "frame-ancestors 'none'" in response.headers['content-security-policy']
        csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', response.text)[1]
        data = {'csrf': csrf, 'revision': '0', 'app_id': '12345', 'app_secret': 'synthetic-new-secret'}
        assert (await c.post('/admin/meta/settings', data=data)).status_code == 403
        assert (await c.post('/admin/meta/settings', data=data | {'csrf': 'forged'}, headers={'Origin': ORIGIN})).status_code == 403
        assert (await c.post('/admin/meta/settings', data=data | {'meta_live_writes': 'true'}, headers={'Origin': ORIGIN})).status_code == 403
        result = await c.post('/admin/meta/settings', data=data, headers={'Origin': ORIGIN});assert result.status_code == 303
        assert 'synthetic-new-secret' not in (await c.get('/admin/')).text
        assert (await c.post('/admin/meta/settings', data=data, headers={'Origin': ORIGIN})).status_code == 409
        assert (await c.get('/approval/')).status_code == 404
        assert (await c.post('/admin/logout', data={'csrf': csrf}, headers={'Origin': ORIGIN})).status_code == 200
        assert (await c.get('/admin/')).status_code == 403
    with sessions() as db:
        row = db.get(SiteConfiguration, 'meta');assert 'synthetic-new-secret' not in row.ciphertext
        assert 'synthetic-new-secret' not in json.dumps([a.details for a in db.scalars(select(Audit))])
    assert not s.meta_live_writes and not s.publishing_enabled and s.approval_mode == 'owner_required'


async def test_admin_routes_disabled_and_no_mcp_admin_tool(sessions):
    s = config(sessions, admin_enabled=False)
    app = create_server(s).streamable_http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        for path in ['/admin', '/admin/', '/admin/oidc/login', '/admin/oidc/callback']:
            assert (await c.get(path)).status_code == 404
        assert (await c.post('/admin/meta/settings', data={})).status_code == 404


def test_admin_session_expiry_and_policy_change(sessions):
    s = config(sessions);backend = AdminSessions(s, sessions);token = backend.create_session('owner-sub')
    with sessions.begin() as db:db.scalar(select(AdminSession)).expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(HubError):backend.authenticate(token)
    token = backend.create_session('owner-sub');s.admin_allowed_subjects = ['other-admin']
    with pytest.raises(HubError):backend.authenticate(token)
    with pytest.raises(HubError):backend.create_session('owner-sub')


def test_admin_and_publisher_are_separate_explicit_roles(sessions):
    s = config(sessions, auth_mode='oauth')
    assert Administration(s, sessions).status('owner-sub')['publishing_permission']
    assert not Administration(s, sessions).status('other-admin')['publishing_permission']
    require_publisher(s, 'owner-sub')
    with pytest.raises(HubError):require_publisher(s, 'other-admin')
    with pytest.raises(HubError):Administration(s, sessions).save('outsider', 0, app_id='12345')


@pytest.mark.parametrize('change', [
    {'admin_allowed_subjects': []}, {'admin_oidc_client_secret': ''}, {'admin_oidc_client_id': ''},
    {'oauth_allowed_client_ids': ['admin-client']}, {'owner_oidc_client_id': 'admin-client'},
    {'admin_allowed_subjects': [' ']}, {'publishing_allowed_subjects': [' owner-sub']},
])
def test_admin_configuration_fails_closed(sessions, change):
    with pytest.raises(ValueError):config(sessions, **change)


async def test_meta_setup_revokes_old_credentials_and_invalidates_login(sessions):
    s = config(sessions, meta_live_reads=True)
    oauth = MetaOAuth(s, sessions, httpx.MockTransport(MetaFixture()))
    await oauth.connect('long-user');link = oauth.issue_login();ticket = parse_qs(urlsplit(link).query)['ticket'][0]
    service = Administration(s, sessions)
    with pytest.raises(HubError):service.save('owner-sub', 0, app_id='67890')
    assert service.save('owner-sub', 0, app_id='67890', app_secret='synthetic-new-secret') == 1
    assert not effective_settings(s, sessions).meta_live_reads
    with sessions() as db:
        assert all(c.revoked for c in db.scalars(select(Credential)))
        assert all(f.consumed for f in db.scalars(select(OAuthFlow)))
    with pytest.raises(HubError):oauth.start(ticket)
    with pytest.raises(HubError, match='setup changed'):await oauth.connect('long-user')
    with pytest.raises(HubError):service.save('owner-sub', 1, live_reads=True)
    assert not s.meta_live_writes


async def test_read_toggle_applies_without_restart_and_checks_connection(sessions):
    s = config(sessions)
    service = Administration(s, sessions)
    with pytest.raises(HubError):service.save('owner-sub', 0, live_reads=True)
    await MetaOAuth(s, sessions, httpx.MockTransport(MetaFixture())).connect('long-user')
    hub = Hub(sessions, EffectiveSettings(s, sessions))
    assert not hub.live_reads
    service.save('owner-sub', 0, live_reads=True);assert hub.live_reads
    assert {a['id'] for a in (await hub.read('list_accounts'))['accounts']} == {'pixelpapa-facebook', 'pixelpapa-instagram'}
    service.save('owner-sub', 1, live_reads=False);assert not hub.live_reads
    assert not s.meta_live_writes and s.approval_mode == 'owner_required'


def test_encrypted_setup_tamper_and_security_field_injection_fail_closed(sessions):
    from app.security.tokens import TokenVault
    s = config(sessions);service = Administration(s, sessions)
    service.save('owner-sub', 0, app_id='12345', app_secret='synthetic-private-value')
    vault = TokenVault([s.encryption_key.get_secret_value()])
    with sessions.begin() as db:
        db.get(SiteConfiguration, 'meta').ciphertext = vault.encrypt({'meta_live_writes': True})
    with pytest.raises(HubError):effective_settings(s, sessions)
    with sessions.begin() as db:db.get(SiteConfiguration, 'meta').ciphertext = 'damaged'
    with pytest.raises(HubError):effective_settings(s, sessions)
    assert not s.meta_live_writes


def test_additive_admin_migration_preserves_existing_data(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text, inspect
    s = settings(database_url='sqlite:///' + str(tmp_path / 'migration.db'))
    monkeypatch.setattr('app.config.Settings', lambda: s)
    cfg = Config('alembic.ini');command.upgrade(cfg, '0005')
    engine = create_engine(s.database_url.get_secret_value())
    with engine.begin() as db:
        db.execute(text("INSERT INTO audit_logs (id,at,subject,operation,outcome,details) VALUES ('existing',CURRENT_TIMESTAMP,'synthetic','existing','success','{}')"))
    command.upgrade(cfg, 'head');command.check(cfg)
    with engine.connect() as db:
        assert db.execute(text('SELECT version_num FROM alembic_version')).scalar() == '0006'
        assert db.execute(text("SELECT operation FROM audit_logs WHERE id='existing'")).scalar() == 'existing'
        assert db.execute(text("SELECT revision FROM site_configuration WHERE id='meta'")).scalar() == 0
    assert 'configuration_revision' in {c['name'] for c in inspect(engine).get_columns('oauth_flows')}
    engine.dispose()


async def test_connect_handoff_requires_admin_csrf_and_stays_on_local_get(sessions):
    s = config(sessions);browser = AdminSessions(s, sessions);token = browser.create_session('owner-sub')
    app = create_server(s).streamable_http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        client.cookies.set('__Secure-socialhub-admin', token)
        payload = {'csrf': browser.csrf(token), 'revision': '0'}
        assert (await client.post('/admin/meta/connect', data=payload)).status_code == 403
        result = await client.post('/admin/meta/connect', data=payload, headers={'Origin': ORIGIN})
        assert result.status_code == 200
        assert 'Continue to Facebook' in result.text
        assert '/meta/oauth/start?ticket=' in result.text
        assert 'fake-app-secret' not in result.text
        assert "form-action 'self'" in result.headers['content-security-policy']
    with sessions() as db:
        assert db.scalar(select(OAuthFlow)).configuration_revision == 0
        assert db.scalar(select(Audit).where(Audit.operation == 'admin_meta_login_issued')).subject == 'owner-sub'


async def test_actual_publication_preparation_rejects_nonpublisher(publisher):
    from test_approval_policy import native_settings
    from test_publishing import prepare
    service, _, calls, _ = publisher
    service.settings = native_settings(service, publishing_allowed_subjects=[])
    with pytest.raises(HubError) as result:await prepare(service)
    assert result.value.code == 'publishing_role_required'
    assert calls == []


async def test_dynamic_setup_does_not_require_nested_database_connections(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import QueuePool
    from app.database.models import Base
    engine = create_engine('sqlite:///' + str(tmp_path / 'small-pool.db'), poolclass=QueuePool,
                           pool_size=1, max_overflow=0, pool_timeout=.1)
    Base.metadata.create_all(engine);factory = sessionmaker(engine, expire_on_commit=False)
    s = config(factory);hub = Hub(factory, EffectiveSettings(s, factory));hub.seed_mocks()
    assert len((await hub.read('list_accounts'))['accounts']) == 2
    value = hub.write('publish_post', 'mock-facebook', {'text': 'synthetic mock only'},
                      'pool-check', 'owner-sub', ['socialhub:mock-write'])
    assert value['mock']
    engine.dispose()
