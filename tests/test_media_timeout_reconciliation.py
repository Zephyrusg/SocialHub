"""An uncertain POST never resends; only a trusted owner can select its outcome."""
from datetime import timedelta
import httpx
import pytest
from sqlalchemy import select,func
from app.core import HubError
from app.connectors.meta import GraphClient,FacebookConnector
from app.connectors.publishing import MetaPublishingConnector
from app.database.models import Publication,PublicationTarget,PublicationApproval,Audit,Post
from test_publishing import publisher,prepare,approve,image_asset


@pytest.mark.parametrize('method,path,read,write',[
    ('GET','me',10,10),('POST','page/feed',10,10),
    ('POST','page/photos',120,30),('POST','page/videos',120,30),
    ('GET','oauth/access_token',10,10)])
async def test_media_ack_wait_is_longer_without_extending_reads_or_text(method,path,read,write):
    seen=[]
    def handler(request):
        seen.append(request)
        assert request.extensions['timeout']=={'connect':10,'read':read,'write':write,'pool':10}
        return httpx.Response(200,json={'id':'existing_remote'})
    g=GraphClient('synthetic','v26.0',transport=httpx.MockTransport(handler),allow_writes=True)
    await g.request(method,path)
    assert len(seen)==1


@pytest.mark.parametrize('kind,phase',[(httpx.ReadTimeout,'read'),(httpx.ConnectTimeout,'connect'),
    (httpx.WriteTimeout,'write'),(httpx.PoolTimeout,'pool')])
async def test_media_timeout_has_safe_phase_but_never_retries(kind,phase):
    seen=[]
    def handler(request):
        seen.append(request);raise kind('SECRET_URL_MUST_NOT_BE_RETAINED')
    g=GraphClient('synthetic','v26.0',transport=httpx.MockTransport(handler),allow_writes=True)
    with pytest.raises(HubError) as e:await g.mutate('POST','page/photos',{'url':'https://example.test/private'})
    assert len(seen)==1 and e.value.code=='api_timeout' and not e.value.retryable
    assert e.value.diagnostics=={'mutation_attempted':True,'timeout_phase':phase}
    assert 'SECRET' not in str(e.value)


async def uncertain_photo(publisher,tmp_path):
    service,adapters,_,_=publisher
    asset=image_asset(service,tmp_path);p=await prepare(service,content_type='image',media_ids=[asset['id']])
    approve(service,p);adapters['facebook'].fail=HubError('api_timeout','Uncertain',diagnostics={'mutation_attempted':True,'timeout_phase':'read'})
    outcome=await service.execute('alice',['socialhub:publish'],p['publication_id'])
    assert outcome['state']=='attention_required' and outcome['targets'][0]['attempts']==1
    return service,outcome


def owned_reader(service,seen,change=None,hook=None):
    post={'id':'page_photo','message':'Explicit preview caption','created_time':service.clock().isoformat()+'Z',
        'permalink_url':'https://www.facebook.com/photo','attachments':{'data':[{'type':'photo'}]}}
    if change:post.update(change)
    def handler(r):
        assert r.method=='GET';seen.append(r.method)
        if hook:hook()
        return httpx.Response(200,json={'data':[post]})
    def factory(account,execute):
        assert not execute
        return MetaPublishingConnector(FacebookConnector(GraphClient('synthetic','v26.0',transport=httpx.MockTransport(handler)),account.remote_id))
    service.connector_factory=factory


async def test_owner_photo_reconciliation_is_get_only_cached_and_not_replayed(publisher,tmp_path,sessions):
    service,p=await uncertain_photo(publisher,tmp_path);seen=[];owned_reader(service,seen)
    # Repeated execution of the uncertain target cannot even attempt a read/write.
    assert (await service.execute('alice',['socialhub:publish'],p['publication_id']))['state']=='attention_required'
    assert seen==[]
    result=await service.reconcile(p['publication_id'],p['targets'][0]['target_id'],'page_photo','trusted-owner',expected_hash=p['request_hash'])
    assert result['state']=='succeeded' and result['targets'][0]['attempts']==1 and result['request_hash']==p['request_hash']
    assert result['targets'][0]['delivery_confirmed'] and result['targets'][0]['retry_at'] is None
    assert seen==['GET']
    await service.refresh_status(p['publication_id'],'alice')
    await service.execute('alice',['socialhub:publish'],p['publication_id'])
    assert seen==['GET']
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Post))==1
        event=db.scalar(select(Audit).where(Audit.operation=='reconcile_publication'))
        assert event.details['mutation_attempted'] is False and event.details['attempts_unchanged']
        sent=db.scalar(select(Audit).where(Audit.operation=='execute_publication'))
        assert sent.details['timeout_phase']=='read'
    with pytest.raises(HubError):await service.reconcile(p['publication_id'],p['targets'][0]['target_id'],'page_photo','trusted-owner')
    assert seen==['GET']


@pytest.mark.parametrize('change',[
    {'message':'Different caption'}, {'attachments':{'data':[]}},
    {'attachments':{'data':[{'type':'video'}]}},
    {'attachments':{'data':[{'type':'photo'},{'type':'photo'}]}},
    {'created_time':'2020-01-01T00:00:00Z'}])
async def test_owner_reconciliation_rejects_wrong_photo_evidence(publisher,tmp_path,change):
    service,p=await uncertain_photo(publisher,tmp_path);seen=[];owned_reader(service,seen,change)
    with pytest.raises(HubError) as e:await service.reconcile(p['publication_id'],p['targets'][0]['target_id'],'page_photo','trusted-owner')
    assert e.value.code=='reconciliation_denied'
    assert service.review(p['publication_id'],'alice')['state']=='attention_required' and seen==['GET']


@pytest.mark.parametrize('reason',['wrong_hash','no_issuer','no_used_grant','no_attempt','active_lease','remote_already_bound'])
async def test_reconciliation_requires_exact_owner_history_and_unclaimed_target(publisher,tmp_path,sessions,reason):
    service,p=await uncertain_photo(publisher,tmp_path);seen=[];owned_reader(service,seen)
    with sessions.begin() as db:
        t=db.get(PublicationTarget,p['targets'][0]['target_id'])
        if reason=='no_used_grant':db.scalar(select(PublicationApproval)).used_at=None
        if reason=='no_attempt':t.attempts=0
        if reason=='active_lease':t.lease_token='active'
        if reason=='remote_already_bound':db.add(Post(account_id='pixelpapa-facebook',remote_id='page_photo',content={'publication_id':'another_request'}))
    with pytest.raises(HubError):await service.reconcile(p['publication_id'],p['targets'][0]['target_id'],'page_photo',
        '' if reason=='no_issuer' else 'trusted-owner',expected_hash='wrong' if reason=='wrong_hash' else p['request_hash'])
    assert seen==(['GET'] if reason=='remote_already_bound' else [])


async def test_reconciliation_rechecks_target_after_remote_read(publisher,tmp_path,sessions):
    service,p=await uncertain_photo(publisher,tmp_path);seen=[]
    def changed():
        with sessions.begin() as db:db.get(PublicationTarget,p['targets'][0]['target_id']).state='failed'
    owned_reader(service,seen,hook=changed)
    with pytest.raises(HubError):await service.reconcile(p['publication_id'],p['targets'][0]['target_id'],'page_photo','trusted-owner')
    assert service.review(p['publication_id'],'alice')['state']=='failed'


async def test_execution_time_not_preparation_time_bounds_reconciliation(publisher,tmp_path,sessions):
    service,p=await uncertain_photo(publisher,tmp_path);seen=[]
    with sessions.begin() as db:db.get(Publication,p['publication_id']).created_at=service.clock()-timedelta(days=1)
    stamp=(service.clock()-timedelta(hours=1)).isoformat()+'Z';owned_reader(service,seen,{'created_time':stamp})
    with pytest.raises(HubError,match='pre-existing'):await service.reconcile(p['publication_id'],p['targets'][0]['target_id'],'page_photo','trusted-owner')


async def test_reconciliation_rechecks_immutable_hash_after_remote_read(publisher,tmp_path,sessions):
    from app.services.publishing import content_hash
    service,p=await uncertain_photo(publisher,tmp_path);seen=[]
    def changed():
        with sessions.begin() as db:
            row=db.get(Publication,p['publication_id']);payload=dict(row.payload)
            payload['request']=dict(payload['request'],text='Changed during external verification')
            row.payload=payload;row.request_hash=content_hash(payload)
    owned_reader(service,seen,hook=changed)
    with pytest.raises(HubError):await service.reconcile(p['publication_id'],p['targets'][0]['target_id'],'page_photo','trusted-owner')
    assert service.review(p['publication_id'],'alice')['state']=='attention_required'
