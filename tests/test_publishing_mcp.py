import json
import httpx
import pytest
from cryptography.fernet import Fernet
from app.config import Settings
from app.database import database
from app.database.models import Base
from app.main import create_server
from app.services.meta_oauth import MetaOAuth
from app.services.publishing import PublishingService
from app.connectors.publishing import MetaPublishingConnector
from app.connectors.meta import GraphClient, FacebookConnector
from test_meta_live import MetaFixture
from test_publishing import FakeAdapter, prepare, approve, publisher


async def test_mcp_prepare_approve_execute_and_legacy_preview(tmp_path,monkeypatch):
    url=f'sqlite:///{tmp_path}/mcp.db';engine,sessions=database(url);Base.metadata.create_all(engine)
    s=Settings(_env_file=None,database_url=url,encryption_key=Fernet.generate_key().decode(),local_token='a'*64,
        meta_app_id='12345',meta_app_secret='fake-app-secret',meta_live_reads=True,publishing_enabled=True,meta_live_writes=True,meta_write_account_ids=["pixelpapa-facebook","pixelpapa-instagram"],media_root=str(tmp_path/'media'))
    await MetaOAuth(s,sessions,httpx.MockTransport(MetaFixture())).connect('long-user')
    calls=[];fake=FakeAdapter('facebook',calls)
    original=PublishingService.context
    def context(self,aid,execute=False):
        account,caps,_=original(self,aid,execute)
        return account,caps,fake
    monkeypatch.setattr(PublishingService,'context',context)
    server=create_server(s).streamable_http_app()
    headers={'Authorization':'Bearer '+'a'*64,'Accept':'application/json, text/event-stream'}
    async with server.router.lifespan_context(server):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),base_url='http://localhost:8787',headers=headers) as client:
            async def rpc(method,params):
                r=await client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
                assert r.status_code==200,r.text
                return json.loads(next(l[6:] for l in r.text.splitlines() if l.startswith('data: ')))['result']
            await rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'pytest','version':'1'}})
            tools=await rpc('tools/list',{})
            names={t['name'] for t in tools['tools']}
            assert len(names)==17
            assert 'get_approval_requirements' in names
            assert not any('approve' in n or 'reconcile' in n for n in names)
            async def call(name,args):
                return (await rpc('tools/call',{'name':name,'arguments':args}))['structuredContent']
            response=await call('publish_post',{'account_id':'pixelpapa-facebook','text':'Review only','idempotency_key':'legacy'})
            assert response['ok'] and response['data']['state']=='prepared' and not calls
            assert response['data']['approval']['preferred_interface']=='chatgpt'
            result=await call('prepare_publication',{'account_ids':['pixelpapa-facebook'],'text':'Approve exact preview','idempotency_key':'new'})
            preview=result['data'];pid=preview['publication_id']
            denied=await call('execute_publication',{'publication_id':pid,'approved':True})
            assert not denied['ok'] and denied['error']['code']=='authorization_required' and not calls
            PublishingService(s,sessions).approve(pid,preview['request_hash'],'test-owner','Explicitly publish the exact displayed preview')
            assert (await call('execute_publication',{'publication_id':pid}))['data']['state']=='in_progress'
            assert (await call('execute_publication',{'publication_id':pid}))['data']['state']=='succeeded'
            assert (await call('get_publication_status',{'publication_id':pid}))['data']['targets'][0]['result']['remote_id']=='facebook_remote'
            assert len(calls)==2


async def test_reconciliation_uses_owned_matching_recent_remote_object(publisher,sessions):
    from app.database.models import PublicationTarget
    service,_,_,_=publisher
    preview=await prepare(service)
    tid=preview['targets'][0]['target_id']
    with sessions.begin() as db:
        db.get(PublicationTarget,tid).state='uncertain'
    def handler(r):
        assert r.method=='GET'
        return httpx.Response(200,json={'data':[{'id':'remote','message':'Explicit preview caption','created_time':service.clock().isoformat()+'Z','permalink_url':'https://www.facebook.com/remote'}]})
    remote=MetaPublishingConnector(FacebookConnector(GraphClient('fake','v26.0',transport=httpx.MockTransport(handler)), '1398970469971456'))
    service.connector_factory=lambda account,execute:remote
    result=await service.reconcile(preview['publication_id'],tid,'remote','owner')
    assert result['state']=='succeeded' and result['targets'][0]['result']['reconciled']


async def test_reconciliation_refuses_original_old_post(publisher,sessions):
    from app.database.models import PublicationTarget
    service,_,_,_=publisher
    preview=await prepare(service)
    tid=preview['targets'][0]['target_id']
    with sessions.begin() as db:db.get(PublicationTarget,tid).state='uncertain'
    def handler(r):
        assert r.method=='GET'
        return httpx.Response(200,json={'data':[{'id':'old_post','message':'Explicit preview caption','created_time':'2020-01-01T00:00:00Z'}]})
    remote=MetaPublishingConnector(FacebookConnector(GraphClient('fake','v26.0',transport=httpx.MockTransport(handler)),'1398970469971456'))
    service.connector_factory=lambda account,execute:remote
    with pytest.raises(Exception,match='pre-existing'):
        await service.reconcile(preview['publication_id'],tid,'old_post','owner')


async def test_chatgpt_policy_uses_signed_client_identity_through_real_mcp(tmp_path,monkeypatch):
    import time
    from types import SimpleNamespace
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from app.security.auth import Verifier
    url=f'sqlite:///{tmp_path}/native-mcp.db';engine,sessions=database(url);Base.metadata.create_all(engine)
    s=Settings(_env_file=None,database_url=url,encryption_key=Fernet.generate_key().decode(),
        oauth_allowed_subjects=['alice'],publishing_allowed_subjects=['alice'],oauth_allowed_client_ids=['real-client','impostor'],auth_mode='oauth',oauth_issuer='https://issuer.example/',oauth_jwks_url='https://issuer.example/keys',
        oauth_audience='https://hub.example/mcp',public_url='https://hub.example/mcp',
        meta_app_id='12345',meta_app_secret='fake-app-secret',meta_live_reads=True,
        publishing_enabled=True,meta_live_writes=True,meta_write_account_ids=["pixelpapa-facebook","pixelpapa-instagram"],approval_mode='chatgpt_native',
        chatgpt_confirmation_tested=True,chatgpt_confirmation_evidence_sha256='e'*64,
        chatgpt_residual_risk_accepted=True,chatgpt_allowed_client_ids=['real-client'],chatgpt_allowed_principals=['alice'],
        media_root=str(tmp_path/'media'))
    await MetaOAuth(s,sessions,httpx.MockTransport(MetaFixture())).connect('long-user')
    calls=[];fake=FakeAdapter('facebook',calls);original=PublishingService.context
    def fake_context(self,aid,execute=False):
        account,caps,_=original(self,aid,execute)
        return account,caps,fake
    monkeypatch.setattr(PublishingService,'context',fake_context)
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    monkeypatch.setattr(jwt.PyJWKClient,'get_signing_key_from_jwt',lambda self,token:SimpleNamespace(key=key.public_key()))
    def bearer(client):
        return jwt.encode({'iss':s.oauth_issuer,'aud':s.oauth_audience,'sub':'alice','azp':client,
            'iat':int(time.time()),'exp':int(time.time())+600,'scope':'socialhub:read socialhub:publish'},key,algorithm='RS256',headers={'typ':'at+jwt'})
    app=create_server(s).streamable_http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost',
            headers={'Authorization':'Bearer '+bearer('impostor'),'Accept':'application/json, text/event-stream'}) as client:
            async def rpc(method,params):
                r=await client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
                assert r.status_code==200,r.text
                return json.loads(next(l[6:] for l in r.text.splitlines() if l.startswith('data: ')))['result']
            await rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'ChatGPT','version':'1'}})
            discovered=(await rpc('tools/list',{}))['tools']
            execute=next(t for t in discovered if t['name']=='execute_publication')
            assert execute['annotations']['readOnlyHint'] is False and execute['annotations']['destructiveHint'] is True
            assert 'approved' not in execute['inputSchema']['properties'] and 'context' not in execute['inputSchema']['properties']
            async def call(name,args):return (await rpc('tools/call',{'name':name,'arguments':args}))['structuredContent']
            p=(await call('prepare_publication',{'account_ids':['pixelpapa-facebook'],'text':'Mock-only exact preview','idempotency_key':'native-test'}))['data']
            required=(await call('get_approval_requirements',{'publication_id':p['publication_id']}))['data']
            assert required['independent_owner_required']
            denied=await call('execute_publication',{'publication_id':p['publication_id'],'approved':True,'oauth_client_id':'real-client'})
            assert denied['error']['code']=='authorization_required' and not calls
            client.headers['Authorization']='Bearer '+bearer('real-client')
            required=(await call('get_approval_requirements',{'publication_id':p['publication_id']}))['data']
            assert not required['independent_owner_required'] and required['native_confirmation_receipt_verified'] is False
            assert (await call('execute_publication',{'publication_id':p['publication_id']}))['data']['state']=='in_progress'
            result=(await call('execute_publication',{'publication_id':p['publication_id']}))['data']
            assert result['state']=='succeeded' and result['approval_channel']=='chatgpt_policy'
            assert not result['independent_owner_approval'] and len(calls)==2
    engine.dispose()
