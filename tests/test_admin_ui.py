"""Dashboard navigation and assets preserve the existing administrator boundary."""
import re

import httpx
import pytest
from sqlalchemy import select

from app.admin_ui import ASSETS, AccountView, ProviderView, provider_card
from app.database.models import SiteConfiguration, Audit
from app.main import create_server
from app.services.administration import AdminSessions
from test_administration import config
from test_production_auth import ORIGIN


async def test_navigation_is_read_only_and_every_page_requires_owner(sessions):
    settings = config(sessions, admin_allowed_subjects=['owner-sub'])
    browser = AdminSessions(settings, sessions)
    cookie = browser.create_session('owner-sub')
    app = create_server(settings).streamable_http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        for path in ['/', '/connections', '/features', '/security', '/settings']:
            assert (await client.get('/admin' + path)).status_code == 403
        client.cookies.set('__Secure-socialhub-admin', cookie)
        for path, heading in [('/', 'Overview'), ('/connections', 'Connections'),
                              ('/features', 'Feature Flags'), ('/security', 'Security'),
                              ('/settings', 'General Settings')]:
            page = await client.get('/admin' + path)
            assert page.status_code == 200 and f'<h1>{heading}</h1>' in page.text
            assert page.text.count('aria-current="page"') == 1
            assert 'Skip to content' in page.text
            assert 'fake-app-secret' not in page.text and 'owner-sub' not in page.text
            assert page.headers['cache-control'] == 'no-store'
            assert "script-src 'self'" in page.headers['content-security-policy']
        assert (await client.get('/admin/unknown')).status_code == 404
    with sessions() as db:
        assert db.get(SiteConfiguration, 'features') is None
        assert db.get(SiteConfiguration, 'meta') is None
        assert not list(db.scalars(select(Audit).where(Audit.operation == 'admin_feature_updated')))
    assert not settings.meta_live_writes and not settings.publishing_enabled


async def test_server_filters_and_untrusted_feedback_are_escaped(sessions):
    settings = config(sessions)
    token = AdminSessions(settings, sessions).create_session('owner-sub')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_server(settings).streamable_http_app()), base_url=ORIGIN) as client:
        client.cookies.set('__Secure-socialhub-admin', token)
        result = await client.get('/admin/features', params={'q': '<img src=x onerror=alert(1)>', 'saved': '<script>alert(1)</script>'})
        assert result.status_code == 200
        assert '<img src=x' not in result.text and '<script>alert' not in result.text
        assert '&lt;img' in result.text and '0 of 11 features' in result.text
        result = await client.get('/admin/features', params={'category': 'Facebook'})
        assert '4 of 11 features' in result.text
        assert re.search(r'<tr data-feature[^>]+data-category="Instagram"[^>]+ hidden>', result.text)
        result = await client.get('/admin/features', params={'state': 'unavailable'})
        assert 'Not implemented' in result.text and 'Scheduling' in result.text


@pytest.mark.parametrize('enabled,status', [(True, 200), (False, 404)])
async def test_only_known_local_assets_are_served(sessions, enabled, status):
    settings = config(sessions, admin_enabled=enabled)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_server(settings).streamable_http_app()), base_url=ORIGIN) as client:
        for name, content in ASSETS.items():
            response = await client.get('/admin/assets/' + name)
            assert response.status_code == status
            if enabled:
                assert response.content == content
                assert response.headers['x-content-type-options'] == 'nosniff'
        assert (await client.get('/admin/assets/unknown.js')).status_code == 404


def test_reusable_provider_presentation_escapes_external_names():
    html = provider_card(ProviderView('<Provider>', 'A & B',
                         (AccountView('<Account>', 'Other', 'not-rendered', False),)))
    assert '&lt;Provider&gt;' in html and '&lt;Account&gt;' in html
    assert '<Account>' not in html and 'not-rendered' not in html
    assert 'Not connected' in html


async def test_new_normalization_switch_shows_its_off_default(sessions):
    settings = config(sessions, media_ingestion_enabled=True,
        media_normalization_enabled=True, media_normalization_secret='s' * 64)
    cookie = AdminSessions(settings, sessions).create_session('owner-sub')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_server(settings).streamable_http_app()), base_url=ORIGIN) as client:
        client.cookies.set('__Secure-socialhub-admin', cookie)
        page = await client.get('/admin/features', params={'q': 'normalization'})
        assert '1 of 11 features' in page.text and 'Off by default' in page.text
        row = re.search(r'<tr data-feature[^>]*data-search="[^"]*normalization[\s\S]*?</tr>', page.text)[0]
        assert 'aria-checked="false"' in row and 'name="enabled" value="true"' in row
    with sessions() as db:assert db.get(SiteConfiguration, 'features') is None
