"""Final-state refresh must never resume or repeat a public mutation."""
import asyncio
from datetime import timedelta
import json
import httpx
import pytest
from sqlalchemy import select, func
from app.core import HubError
from app.connectors.meta import GraphClient, FacebookConnector
from app.connectors.publishing import MetaPublishingConnector
from app.database.models import PublicationTarget, Publication, Post, Audit
from test_publishing import publisher, prepare, approve

REMOTE = 'page_post'


def use_meta(service, handler):
    def factory(account, execute):
        return MetaPublishingConnector(FacebookConnector(GraphClient('synthetic','v26.0',
            transport=httpx.MockTransport(handler),allow_writes=execute),account.remote_id))
    service.connector_factory = factory


async def delivered(service, sessions, **checkpoint):
    p = await prepare(service)
    with sessions.begin() as db:
        target = db.get(PublicationTarget,p['targets'][0]['target_id'])
        target.state, target.attempts = 'processing', 1
        target.checkpoint = {'stage':'delivered','remote_id':REMOTE,**checkpoint}
    return p


async def test_one_execute_finishes_real_meta_ack_without_second_post(publisher, sessions):
    service,_,_,_=publisher;seen=[]
    def handler(r):
        seen.append((r.method,r.url.path))
        if r.method=='POST':
            assert r.url.path.endswith('/feed')
            return httpx.Response(200,json={'id':REMOTE})
        assert r.method=='GET' and r.url.path.endswith('/'+REMOTE)
        return httpx.Response(200,json={'id':REMOTE,'permalink_url':'https://www.facebook.com/post'})
    use_meta(service,handler)
    p=await prepare(service);approve(service,p)
    result=await service.execute('alice',['socialhub:publish'],p['publication_id'])
    assert result['state']=='succeeded' and result['targets'][0]['attempts']==1
    assert result['targets'][0]['delivery_confirmed']
    await service.execute('alice',['socialhub:publish'],p['publication_id'])
    await service.refresh_status(p['publication_id'],'alice')
    assert [m for m,_ in seen]==['POST','GET']
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Post))==1
        event=db.scalar(select(Audit).where(Audit.operation=='finalize_publication'))
        assert event.details['mutation_attempted'] is False


async def test_status_finishes_existing_ack_with_all_write_flags_off(publisher,sessions):
    service,_,_,_=publisher;p=await delivered(service,sessions);seen=[]
    def handler(r):
        assert r.method=='GET';seen.append(r)
        return httpx.Response(200,json={'id':REMOTE,'permalink_url':'https://www.facebook.com/post'})
    use_meta(service,handler)
    service.settings.meta_live_writes=service.settings.publishing_enabled=False
    service.claim=lambda *a,**k:pytest.fail('Status must never claim a write')
    result=await service.refresh_status(p['publication_id'],'alice')
    assert result['state']=='succeeded' and result['targets'][0]['attempts']==1 and len(seen)==1
    assert result['targets'][0]['result']['remote_id']==REMOTE


async def test_metadata_timeout_keeps_delivery_and_backs_off_without_reposting(publisher,sessions):
    service,_,_,now=publisher;p=await delivered(service,sessions);seen=[];ready=[False]
    def handler(r):
        assert r.method=='GET';seen.append(r)
        if not ready[0]:raise httpx.ReadTimeout('Do not expose request or credentials')
        return httpx.Response(200,json={'id':REMOTE,'permalink_url':'https://www.facebook.com/post'})
    use_meta(service,handler)
    result=await service.refresh_status(p['publication_id'],'alice')
    target=result['targets'][0]
    assert target['state']=='processing' and target['delivery_confirmed'] and target['error']['code']=='api_timeout'
    assert target['checkpoint']['remote_id']==REMOTE and target['attempts']==1 and target['retry_at']
    count=len(seen);await service.refresh_status(p['publication_id'],'alice');assert len(seen)==count
    ready[0]=True;now[0]+=timedelta(seconds=61)
    result=await service.refresh_status(p['publication_id'],'alice')
    assert result['state']=='succeeded' and result['targets'][0]['error'] is None
    assert result['targets'][0]['attempts']==1


@pytest.mark.parametrize('state,stage',[('prepared',None),('uncertain','delivered'),('sending','delivered'),
    ('failed','delivered'),('processing','ig_container'),('processing','video_processing')])
async def test_status_never_resumes_unacknowledged_uncertain_or_active_work(publisher,sessions,state,stage):
    service,_,_,_=publisher;p=await delivered(service,sessions)
    with sessions.begin() as db:
        t=db.get(PublicationTarget,p['targets'][0]['target_id']);t.state=state;t.checkpoint={'stage':stage,'remote_id':REMOTE}
    use_meta(service,lambda r:pytest.fail('This state must not perform a network request'))
    result=await service.refresh_status(p['publication_id'],'alice')
    assert result['targets'][0]['state']==state


async def test_photo_finalization_preserves_private_checkpoint_lookup_kind(publisher,sessions):
    service,_,_,_=publisher;p=await delivered(service,sessions,lookup_kind='photo')
    def handler(r):
        assert r.method=='GET' and r.url.params['fields']=='id,link'
        return httpx.Response(200,json={'id':REMOTE,'link':'https://www.facebook.com/photo'})
    use_meta(service,handler)
    result=await service.refresh_status(p['publication_id'],'alice')
    assert result['state']=='succeeded' and result['targets'][0]['result']['permalink'].endswith('/photo')


async def test_status_ownership_and_hash_checked_before_network(publisher,sessions):
    service,_,_,_=publisher;p=await delivered(service,sessions)
    use_meta(service,lambda r:pytest.fail('Denied preview must not reach Meta'))
    with pytest.raises(HubError):await service.refresh_status(p['publication_id'],'bob')
    with sessions.begin() as db:
        item=db.get(Publication,p['publication_id']);item.payload={'request':{'text':'tampered'}}
    with pytest.raises(HubError) as error:await service.refresh_status(p['publication_id'],'alice')
    assert error.value.code=='preview_changed'


async def test_concurrent_refreshes_do_not_duplicate_cached_posts_or_success_audit(publisher,sessions):
    service,_,_,_=publisher;p=await delivered(service,sessions);arrived=[0];both=asyncio.Event()
    async def handler(r):
        assert r.method=='GET';arrived[0]+=1
        if arrived[0]==2:both.set()
        await asyncio.wait_for(both.wait(),2)
        return httpx.Response(200,json={'id':REMOTE,'permalink_url':'https://www.facebook.com/post'})
    use_meta(service,handler)
    results=await asyncio.gather(*(service.refresh_status(p['publication_id'],'alice') for _ in range(2)))
    assert all(p['state']=='succeeded' for p in results)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Post))==1
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.operation=='finalize_publication'))==1


async def test_mcp_status_tool_refreshes_delivery_without_write_execution(publisher,sessions,monkeypatch):
    from pydantic import SecretStr
    from app.main import create_server
    from app.services.publishing import PublishingService
    service,_,_,_=publisher
    p=await service.prepare('local-developer','status-mcp',{'account_ids':['pixelpapa-facebook'],'text':'Explicit preview caption'})
    with sessions.begin() as db:
        t=db.get(PublicationTarget,p['targets'][0]['target_id']);t.state='processing';t.attempts=1;t.checkpoint={'stage':'delivered','remote_id':REMOTE}
    original=PublishingService.context
    def context(self,aid,execute=False):
        account,caps,_=original(self,aid,execute=False)
        assert not execute
        def handler(r):
            assert r.method=='GET'
            return httpx.Response(200,json={'id':REMOTE,'permalink_url':'https://www.facebook.com/post'})
        return account,caps,MetaPublishingConnector(FacebookConnector(GraphClient('synthetic','v26.0',transport=httpx.MockTransport(handler)),account.remote_id))
    monkeypatch.setattr(PublishingService,'context',context)
    s=service.settings.model_copy(update={'database_url':SecretStr(str(sessions.kw['bind'].url)),'meta_live_writes':False,'publishing_enabled':False})
    app=create_server(s).streamable_http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost:8787',headers={'Authorization':'Bearer '+'a'*64,'Accept':'application/json, text/event-stream'}) as client:
            async def rpc(method,params):
                r=await client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':method,'params':params});assert r.status_code==200
                return json.loads(next(l[6:] for l in r.text.splitlines() if l.startswith('data: ')))['result']
            await rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'pytest','version':'1'}})
            tools=(await rpc('tools/list',{}))['tools']
            assert next(t for t in tools if t['name']=='get_publication_status')['annotations']['readOnlyHint']
            result=(await rpc('tools/call',{'name':'get_publication_status','arguments':{'publication_id':p['publication_id']}}))['structuredContent']
            assert result['ok'] and result['data']['state']=='succeeded'
