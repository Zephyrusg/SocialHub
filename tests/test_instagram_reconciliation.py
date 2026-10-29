"""Known published containers are recovered with GETs, never another media_publish."""
from copy import deepcopy
from datetime import timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import select, func

from app.core import HubError
from app.database.models import Audit, Post, Publication, PublicationTarget
from app.connectors.meta import GraphClient, InstagramConnector
from app.connectors.meta_policy import INSTAGRAM_ID
from app.connectors.publishing import MetaPublishingConnector
from test_reels import setup_reels
from test_publishing import publisher


async def uncertain(publisher, *, caption='Exact Reel caption'):
    service,meta,preview,asset,execute,poll=await setup_reels(publisher,['pixelpapa-instagram'],caption=caption)
    meta.fail_publish='instagram'
    await execute();await execute();await poll();result=await execute()
    assert result['state']=='attention_required' and result['targets'][0]['state']=='uncertain'
    assert meta.calls['instagram:publish']==1
    target=result['targets'][0]
    return service,meta,preview,asset,target,execute,poll


def reader(service,preview,*,change=None,container='PUBLISHED',extra=None,remote='303',paging=False,on_read=None):
    seen=[]
    post={'id':'303','caption':preview['preview']['request']['text'],'media_type':'VIDEO','media_product_type':'REELS',
          'permalink':'https://www.instagram.com/reel/example/',
          'timestamp':service.clock().replace(tzinfo=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S%z')}
    if change:post.update(change)
    def handle(request):
        assert request.method=='GET'
        seen.append(request)
        if on_read:on_read(request)
        if request.url.path.endswith('/202'):
            return httpx.Response(200,json={'id':'202','status_code':container})
        assert request.url.path.endswith('/'+INSTAGRAM_ID+'/media')
        assert 'media_product_type' in request.url.params['fields']
        payload={'data':[post]+(extra or [])}
        if paging:payload['paging']={'next':'https://graph.facebook.com/next','cursors':{'after':'repeat'}}
        return httpx.Response(200,json=payload)
    adapter=MetaPublishingConnector(InstagramConnector(GraphClient('synthetic-token','v26.0',
        transport=httpx.MockTransport(handle),allow_writes=False),INSTAGRAM_ID))
    service.connector_factory=lambda account,execute:adapter
    return seen,post


async def test_known_instagram_reel_reconciles_without_republishing(publisher):
    service,meta,preview,asset,target,execute,poll=await uncertain(publisher)
    before=deepcopy(service.review(preview['publication_id'],None))
    seen,_=reader(service,preview)
    result=await service.reconcile(preview['publication_id'],target['target_id'],'303','operator:test',expected_hash=preview['request_hash'])
    row=result['targets'][0]
    assert result['state']==row['state']=='succeeded' and row['delivery_confirmed']
    assert row['attempts']==3 and row['next_action']=='none' and row['retry_at'] is None and row['error'] is None
    assert row['checkpoint']['container_id']=='202' and row['checkpoint']['remote_id']=='303'
    assert row['result']['remote_id']=='303' and row['result']['reconciled']
    assert result['preview']==before['preview'] and result['request_hash']==before['request_hash']
    assert result['preview']['assets'][0]['id']==asset['id']
    assert len(seen)==2 and meta.calls['instagram:publish']==1
    assert (await execute())['state']=='succeeded' and meta.calls['instagram:publish']==1
    with service.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Publication))==1
        assert db.scalar(select(func.count()).select_from(Post))==1
        history=list(db.scalars(select(Audit).where(Audit.operation=='execute_publication',Audit.outcome=='uncertain')))
        assert len(history)==1
        audit=db.scalar(select(Audit).where(Audit.operation=='reconcile_publication'))
        assert audit.subject=='operator:test' and audit.details['container_id']=='202'
        assert audit.details['mutation_attempted'] is False and audit.details['attempts_unchanged']


@pytest.mark.parametrize('change',[
    {'caption':'Different'}, {'media_product_type':'FEED'}, {'media_type':'IMAGE'},
    {'timestamp':'2020-01-01T00:00:00Z'}, {'timestamp':'2099-01-01T00:00:00Z'},
    {'timestamp':'invalid'}, {'timestamp':'2026-01-01T00:00:00'},
    {'id':'another-account-id'}, {'permalink':'https://evil.invalid/reel/example/'},
    {'permalink':'https://www.instagram.com/reel/example/?capability=never-expose'},
])
async def test_wrong_or_unverifiable_reel_does_not_change_state(publisher,change):
    service,meta,preview,_,target,_,_=await uncertain(publisher)
    seen,_=reader(service,preview,change=change)
    with pytest.raises(HubError):
        await service.reconcile(preview['publication_id'],target['target_id'],'303','operator:test',expected_hash=preview['request_hash'])
    after=service.review(preview['publication_id'],None)['targets'][0]
    assert after['state']=='uncertain' and after['attempts']==3 and meta.calls['instagram:publish']==1
    with service.sessions() as db:assert db.scalar(select(func.count()).select_from(Post))==0


@pytest.mark.parametrize('status',['FINISHED','IN_PROGRESS','ERROR','EXPIRED'])
async def test_unpublished_container_cannot_prove_delivery(publisher,status):
    service,meta,preview,_,target,_,_=await uncertain(publisher)
    seen,_=reader(service,preview,container=status)
    with pytest.raises(HubError):await service.reconcile(preview['publication_id'],target['target_id'],'303','operator:test')
    assert len(seen)==1 and meta.calls['instagram:publish']==1


async def test_ambiguous_matching_reels_are_rejected(publisher):
    service,meta,preview,_,target,_,_=await uncertain(publisher)
    _,post=reader(service,preview)
    seen,_=reader(service,preview,extra=[post|{'id':'304'}])
    with pytest.raises(HubError):await service.reconcile(preview['publication_id'],target['target_id'],'303','operator:test')
    assert service.review(preview['publication_id'],None)['targets'][0]['state']=='uncertain'


async def test_repeated_pagination_and_changed_checkpoint_are_rejected(publisher):
    service,meta,preview,_,target,_,_=await uncertain(publisher)
    reader(service,preview,paging=True)
    with pytest.raises(HubError):await service.reconcile(preview['publication_id'],target['target_id'],'303','operator:test')
    def change_checkpoint(request):
        if request.url.path.endswith('/media'):
            with service.sessions.begin() as db:
                row=db.get(PublicationTarget,target['target_id']);row.checkpoint=dict(row.checkpoint,polls=99)
    reader(service,preview,on_read=change_checkpoint)
    with pytest.raises(HubError):await service.reconcile(preview['publication_id'],target['target_id'],'303','operator:test')
    assert service.review(preview['publication_id'],None)['targets'][0]['state']=='uncertain'


async def test_missing_finish_evidence_and_changed_hash_are_rejected(publisher):
    service,meta,preview,_,target,_,_=await uncertain(publisher)
    seen,_=reader(service,preview)
    with pytest.raises(HubError):await service.reconcile(preview['publication_id'],target['target_id'],'303','operator:test',expected_hash='wrong')
    assert not seen
    with service.sessions.begin() as db:
        for event in db.scalars(select(Audit).where(Audit.operation=='execute_publication',Audit.outcome=='uncertain')):db.delete(event)
    with pytest.raises(HubError):await service.reconcile(preview['publication_id'],target['target_id'],'303','operator:test')
    assert not seen and meta.calls['instagram:publish']==1


async def test_instagram_final_publish_gets_media_deadline_and_no_retry():
    seen=[]
    def handle(request):
        seen.append(request)
        assert request.extensions['timeout']=={'connect':10,'read':120,'write':30,'pool':10}
        raise httpx.ReadTimeout('private-provider-detail')
    client=GraphClient('synthetic-token','v26.0',transport=httpx.MockTransport(handle),allow_writes=True)
    with pytest.raises(HubError) as error:
        await client.mutate('POST',INSTAGRAM_ID+'/media_publish',{'creation_id':'202'})
    assert error.value.code=='api_timeout' and not error.value.retryable and len(seen)==1
    assert error.value.diagnostics['timeout_phase']=='read' and 'private-provider-detail' not in error.value.message


async def test_empty_caption_and_disabled_write_gates_still_allow_read_recovery(publisher):
    service,meta,preview,_,target,_,_=await uncertain(publisher,caption='')
    reader(service,preview,change={'caption':None})
    service.settings.meta_live_writes=False;service.settings.publishing_enabled=False
    result=await service.reconcile(preview['publication_id'],target['target_id'],'303','operator:test',expected_hash=preview['request_hash'])
    assert result['state']=='succeeded' and meta.calls['instagram:publish']==1


async def test_remote_id_not_owned_or_already_bound_is_rejected(publisher):
    service,meta,preview,_,target,_,_=await uncertain(publisher)
    seen,_=reader(service,preview)
    with pytest.raises(HubError):await service.reconcile(preview['publication_id'],target['target_id'],'999','operator:test')
    assert all('/999' not in r.url.path for r in seen)
    with service.sessions.begin() as db:
        db.add(Post(account_id='pixelpapa-instagram',remote_id='303',content={'publication_id':'another-publication'}))
    with pytest.raises(HubError):await service.reconcile(preview['publication_id'],target['target_id'],'303','operator:test')
    assert service.review(preview['publication_id'],None)['targets'][0]['state']=='uncertain'
    assert meta.calls['instagram:publish']==1


async def test_reconciling_instagram_does_not_repeat_completed_facebook(publisher):
    service,meta,preview,_,execute,poll=await setup_reels(publisher)
    meta.fail_publish='instagram'
    await execute();await execute();await poll();await execute();state=await poll()
    facebook=next(t for t in state['targets'] if t['account_id']=='pixelpapa-facebook')
    instagram=next(t for t in state['targets'] if t['account_id']=='pixelpapa-instagram')
    assert facebook['state']=='succeeded' and instagram['state']=='uncertain'
    reader(service,preview)
    result=await service.reconcile(preview['publication_id'],instagram['target_id'],'303','operator:test',expected_hash=preview['request_hash'])
    assert result['state']=='succeeded'
    assert next(t for t in result['targets'] if t['account_id']=='pixelpapa-facebook')==facebook
    assert (await execute())['state']=='succeeded'
    assert meta.calls['facebook:publish']==meta.calls['instagram:publish']==1
