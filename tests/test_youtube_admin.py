"""YouTube settings inherit the existing owner, origin and CSRF boundary."""
import httpx
import pytest
from sqlalchemy import select
from app.main import create_server
from app.services.administration import AdminSessions
from app.services.youtube_oauth import YouTubeSetup
from app.database.models import Credential
from test_administration import config
from test_production_auth import ORIGIN
from test_youtube import CLIENT,SECRET,CID

async def test_youtube_forms_require_owner_origin_csrf_and_hide_secret(sessions):
    s=config(sessions,youtube_enabled=True,youtube_channel_ids=[CID]);admin=AdminSessions(s,sessions);cookie=admin.create_session('owner-sub')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_server(s).streamable_http_app()),base_url=ORIGIN) as c:
        data={'csrf':admin.csrf(cookie),'revision':'0','client_id':CLIENT,'client_secret':SECRET}
        assert (await c.post('/admin/youtube/settings',data=data,headers={'Origin':ORIGIN})).status_code==403
        c.cookies.set('__Secure-socialhub-admin',cookie)
        assert (await c.post('/admin/youtube/settings',data=data,headers={'Origin':'https://evil.example.test'})).status_code==403
        assert (await c.post('/admin/youtube/settings',data=dict(data,csrf='x'*64),headers={'Origin':ORIGIN})).status_code==403
        r=await c.post('/admin/youtube/settings',data=data,headers={'Origin':ORIGIN});assert r.status_code==303
        page=await c.get('/admin/connections');assert page.status_code==200 and 'Google / YouTube' in page.text
        assert SECRET not in page.text and 'name="client_secret"' in page.text and 'name="client_secret" value=' not in page.text
        assert 'Not authorized' in page.text
        assert (await c.post('/admin/youtube/connect',data={'csrf':admin.csrf(cookie),'revision':'1'},headers={'Origin':ORIGIN})).status_code==200
        assert c.cookies.get('__Secure-socialhub-youtube')
        assert (await c.post('/admin/youtube/settings',data=dict(data,revision='0'),headers={'Origin':ORIGIN})).status_code==400
        # No uncontrolled remote OAuth follow, and no write operation.
    with sessions() as db:assert not list(db.scalars(select(Credential).where(Credential.account_id=='pixelpapa-youtube')))

async def test_youtube_callback_rejects_bad_state_and_untrusted_proxy(sessions):
    s=config(sessions,youtube_enabled=True,youtube_channel_ids=[CID])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_server(s).streamable_http_app()),base_url=ORIGIN) as c:
        for path in ['/youtube/oauth/callback','/youtube/oauth/callback?state=a&state=b&code=x','/youtube/oauth/callback?state=untrusted-state&code=x']:
            r=await c.get(path);assert r.status_code==400 and 'synthetic' not in r.text
        assert (await c.get('/admin/youtube/avatar')).status_code==404
