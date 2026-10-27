import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select,func
from app.config import Settings
from app.core import HubError
from app.database import database
from app.database.models import Base, PublicationApproval, ApprovalPortalSession, Audit, utcnow
from app.main import create_server
from app.services.approval_portal import OwnerPortal, token_hash
from app.services.publishing import PublishingService
from app.services.meta_oauth import MetaOAuth
from test_meta_live import MetaFixture
from test_publishing import FakeAdapter, prepare, image_asset

ORIGIN='https://localhost:8788'
HEADERS={'X-Forwarded-Proto':'https'}


@pytest.fixture
async def portal(tmp_path):
    url=f'sqlite:///{tmp_path}/portal.db'
    engine,sessions=database(url);Base.metadata.create_all(engine)
    s=Settings(_env_file=None,database_url=url,encryption_key=Fernet.generate_key().decode(),local_token='a'*64,
        meta_app_id='12345',meta_app_secret='fake-app-secret',meta_live_reads=True,publishing_enabled=True,
        meta_live_writes=True,meta_write_account_ids=["pixelpapa-facebook","pixelpapa-instagram"],media_root=str(tmp_path/'media'),approval_portal_enabled=True)
    await MetaOAuth(s,sessions,httpx.MockTransport(MetaFixture())).connect('long-user')
    calls=[];service=PublishingService(s,sessions,lambda a,e:FakeAdapter(a.platform,calls))
    preview=await prepare(service,key='portal')
    app=create_server(s).streamable_http_app()
    # Portal custom routes do not start MCP sessions. Keep AnyIO cancel scopes
    # out of yielding fixtures: pytest finalizes them in a different task.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN,headers=HEADERS) as client:
        yield s,sessions,service,preview,OwnerPortal(s,sessions),client,calls
    engine.dispose()


async def login(portal):
    _,_,_,p,owner,client,_=portal
    url=owner.issue_login('independent-owner')
    response=await client.get(url)
    assert response.status_code==303
    cookie=response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'secure' in cookie and 'samesite=lax' in cookie
    review=await client.get('/approval/publications/'+p['publication_id'])
    assert review.status_code==200
    csrf=re.search('name="csrf" value="([a-f0-9]+)"',review.text)[1]
    return csrf,url


async def test_portal_independent_grant_and_revoke_share_publishing_state(portal):
    _,sessions,service,p,_,client,calls=portal
    csrf,_=await login(portal)
    path='/approval/publications/'+p['publication_id']
    data={'csrf':csrf,'request_hash':p['request_hash'],'instruction':'Publish this exact reviewed content'}
    response=await client.post(path+'/approve',data=data,headers={'Origin':ORIGIN})
    assert response.status_code==200 and 'No content was published' in response.text and not calls
    with sessions() as db:
        grant=db.scalar(select(PublicationApproval))
        assert grant.channel=='owner_portal' and grant.principal=='alice'
        assert db.scalar(select(Audit).where(Audit.operation=='approve_publication')).details['channel']=='owner_portal'
    assert service.review(p['publication_id'],'alice')['independent_owner_approval']
    response=await client.post(path+'/revoke',data={'csrf':csrf,'request_hash':p['request_hash']},headers={'Origin':ORIGIN})
    assert response.status_code==200
    with pytest.raises(HubError):
        await service.execute('alice',['socialhub:publish'],p['publication_id'])
    assert not calls


@pytest.mark.parametrize('change',[{'csrf':'wrong'},{'csrf':'é'*64},{'request_hash':'a'*64},{'instruction':''}])
async def test_forged_forms_cannot_issue_grant(portal,change):
    _,sessions,_,p,_,client,calls=portal
    csrf,_=await login(portal)
    data={'csrf':csrf,'request_hash':p['request_hash'],'instruction':'Exact instruction'}|change
    response=await client.post('/approval/publications/'+p['publication_id']+'/approve',data=data,headers={'Origin':ORIGIN})
    assert response.status_code==403 and not calls
    with sessions() as db:assert db.scalar(select(PublicationApproval)) is None


async def test_origin_content_type_duplicate_fields_and_get_are_refused(portal):
    _,sessions,_,p,_,client,calls=portal
    csrf,_=await login(portal)
    path='/approval/publications/'+p['publication_id']+'/approve'
    data={'csrf':csrf,'request_hash':p['request_hash'],'instruction':'Explicit instruction'}
    assert (await client.post(path,data=data,headers={'Origin':'https://evil.example'})).status_code==403
    assert (await client.post(path,data=data)).status_code==403
    assert (await client.post(path,json=data,headers={'Origin':ORIGIN})).status_code==403
    assert (await client.get(path)).status_code==405
    body=f'csrf={csrf}&csrf={csrf}&request_hash={p["request_hash"]}&instruction=confirm'
    assert (await client.post(path,content=body,headers={'Origin':ORIGIN,'Content-Type':'application/x-www-form-urlencoded'})).status_code==403
    assert (await client.post(path,content='x'*9000,headers={'Origin':ORIGIN,'Content-Type':'application/x-www-form-urlencoded'})).status_code==403
    with sessions() as db:assert db.scalar(select(PublicationApproval)) is None
    assert not calls


async def test_mcp_bearer_is_not_owner_login_and_https_is_required(portal):
    _,_,_,_,_,client,_=portal
    assert (await client.get('/approval/',headers={'Authorization':'Bearer '+'a'*64})).status_code==403
    await login(portal)
    assert (await client.get('/approval/',headers={'X-Forwarded-Proto':'http'})).status_code==403
    assert (await client.get('/approval/',headers={'Host':'evil.example'})).status_code==421


async def test_one_use_login_hashes_logout_and_disabled_mode(portal):
    s,sessions,_,_,_,client,_=portal
    csrf,url=await login(portal)
    assert (await client.get(url)).status_code==403
    ticket=parse_qs(urlparse(url).query)['ticket'][0]
    with sessions() as db:
        row=db.scalar(select(ApprovalPortalSession))
        assert row.ticket_hash==token_hash(ticket) and row.consumed
        assert ticket not in row.browser_hash
    response=await client.post('/approval/logout',data={'csrf':csrf},headers={'Origin':ORIGIN})
    assert response.status_code==200
    assert (await client.get('/approval/')).status_code==403
    s.approval_portal_enabled=False
    assert (await client.get(url)).status_code==404


async def test_session_expiry_and_concurrent_ticket_consumption(portal):
    s,sessions,_,_,_,_,_=portal
    now=[utcnow()];owner=OwnerPortal(s,sessions,clock=lambda:now[0])
    ticket=parse_qs(urlparse(owner.issue_login('owner')).query)['ticket'][0]
    def consume(_):
        try:return owner.start(ticket)
        except HubError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:tokens=list(pool.map(consume,range(2)))
    tokens=[t for t in tokens if t];assert len(tokens)==1
    assert owner.authenticate(tokens[0])=='owner'
    now[0]+=timedelta(minutes=16)
    with pytest.raises(HubError):owner.authenticate(tokens[0])


async def test_exact_asset_preview_and_untrusted_text_escaping(portal,tmp_path):
    _,_,service,_,_,client,calls=portal
    csrf,_=await login(portal)
    asset=image_asset(service,tmp_path)
    from app.models_publication import PublicationRequest
    p=await service.prepare('alice','image',PublicationRequest(account_ids=['pixelpapa-facebook'],content_type='image',media_ids=[asset['id']],text='<script>alert(1)</script>'))
    path='/approval/publications/'+p['publication_id']
    response=await client.get(path)
    assert '&lt;script&gt;' in response.text and '<script>' not in response.text
    assert 'frame-ancestors' in response.headers['content-security-policy'] and response.headers['cache-control']=='no-store'
    response=await client.get(path+'/media/'+asset['id'])
    assert response.status_code==200 and response.content==Path('tests/fixtures/portrait.jpg').read_bytes()
    assert (await client.get(path+'/media/not-referenced')).status_code==403
    assert not calls


async def test_portal_keeps_existing_live_flag_approval_guard(portal):
    s,sessions,_,p,_,client,_=portal
    csrf,_=await login(portal);s.meta_live_writes=False;s.publishing_enabled=False
    response=await client.post('/approval/publications/'+p['publication_id']+'/approve',
        data={'csrf':csrf,'request_hash':p['request_hash'],'instruction':'Exact instruction'},headers={'Origin':ORIGIN})
    assert response.status_code==403 and 'live_writes_disabled' in response.text
    with sessions() as db:assert db.scalar(select(PublicationApproval)) is None

async def test_portal_form_referrer_policy_preserves_origin_without_query_leakage(portal):
    _,_,_,p,_,client,_=portal
    await login(portal)
    response=await client.get('/approval/publications/'+p['publication_id'])
    assert response.headers['referrer-policy']=='strict-origin'
