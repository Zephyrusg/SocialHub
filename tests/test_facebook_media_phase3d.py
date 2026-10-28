from datetime import timedelta
from pathlib import Path
import httpx
import pytest
from sqlalchemy import select
from app.core import HubError
from app.database.models import PublicationTarget
from app.connectors.meta import GraphClient,FacebookConnector
from app.connectors.publishing import MetaPublishingConnector
from test_publishing import publisher,image_asset,prepare,approve


async def test_default_media_write_gate_blocks_before_any_claim_but_text_still_works(publisher,tmp_path,sessions):
    service,_,calls,_=publisher
    asset=image_asset(service,tmp_path)
    preview=await prepare(service,content_type='image',media_ids=[asset['id']])
    service.settings.facebook_media_publishing_enabled=False
    with pytest.raises(HubError) as error:await service.execute('alice',['socialhub:publish'],preview['publication_id'])
    assert error.value.code=='facebook_media_publishing_disabled' and not calls
    assert not service.review(preview['publication_id'],'alice')['live_writes_enabled']
    with sessions() as db:assert db.get(PublicationTarget,preview['targets'][0]['target_id']).attempts==0
    text=await prepare(service,key='text');approve(service,text)
    assert (await service.execute('alice',['socialhub:publish'],text['publication_id']))['state']=='in_progress'
    assert calls[0][0]=='facebook'


async def test_facebook_image_caption_prepare_publish_and_finalize_once(publisher,tmp_path):
    service,_,_,_=publisher;seen=[]
    def factory(account,execute):
        def handler(r):
            seen.append((r.method,r.url.path))
            if r.method=='POST':
                assert r.url.path.endswith('/photos') and b'message=Explicit+preview+caption' in r.content
                assert b'url=https%3A%2F%2Fmedia.example.test' in r.content
                return httpx.Response(200,json={'id':'photo','post_id':'page_photo'})
            assert r.method=='GET' and r.url.path.endswith('/page_photo')
            return httpx.Response(200,json={'id':'page_photo','permalink_url':'https://www.facebook.com/photo'})
        return MetaPublishingConnector(FacebookConnector(GraphClient('synthetic','v26.0',transport=httpx.MockTransport(handler),allow_writes=execute),account.remote_id))
    service.connector_factory=factory
    asset=image_asset(service,tmp_path);p=await prepare(service,content_type='image',media_ids=[asset['id']]);approve(service,p)
    result=await service.execute('alice',['socialhub:publish'],p['publication_id'])
    assert result['state']=='succeeded' and result['targets'][0]['result']['remote_id']=='page_photo'
    await service.refresh_status(p['publication_id'],'alice')
    assert [m for m,_ in seen]==['POST','GET']


async def test_video_upload_and_read_only_status_polling_without_reupload(publisher,tmp_path):
    import shutil
    if not shutil.which('ffprobe'):pytest.skip('ffprobe runs in Docker')
    service,_,_,now=publisher;seen=[];ready=[False]
    def factory(account,execute):
        def handler(r):
            seen.append(r.method)
            if r.method=='POST':
                assert r.url.path.endswith('/videos') and b'description=Explicit+preview+caption' in r.content and b'file_url=' in r.content
                return httpx.Response(200,json={'id':'video_id'})
            assert r.method=='GET' and r.url.path.endswith('/video_id')
            return httpx.Response(200,json={'id':'video_id','status':{'video_status':'ready' if ready[0] else 'processing'},'permalink_url':'https://www.facebook.com/video'})
        return MetaPublishingConnector(FacebookConnector(GraphClient('synthetic','v26.0',transport=httpx.MockTransport(handler),allow_writes=execute),account.remote_id))
    service.connector_factory=factory
    path=tmp_path/'clip.mp4';path.write_bytes(Path('tests/fixtures/reel-h264-3s.mp4').read_bytes());asset=service.media.save(path,'alice')
    p=await prepare(service,content_type='video',media_ids=[asset['id']]);approve(service,p)
    result=await service.execute('alice',['socialhub:publish'],p['publication_id'])
    assert result['state']=='in_progress' and seen==['POST']
    await service.refresh_status(p['publication_id'],'alice');assert seen==['POST']
    (Path(service.settings.media_root)/asset['id']).unlink()
    now[0]+=timedelta(seconds=61)
    result=await service.execute('alice',['socialhub:publish'],p['publication_id'])
    assert result['state']=='in_progress' and seen==['POST','GET']
    assert result['targets'][0]['attempts']==1
    service.settings.meta_live_writes=service.settings.publishing_enabled=False
    ready[0]=True;now[0]+=timedelta(seconds=61)
    result=await service.refresh_status(p['publication_id'],'alice')
    assert result['state']=='succeeded' and seen==['POST','GET','GET']
    assert result['targets'][0]['attempts']==1 and result['targets'][0]['result']['permalink'].endswith('/video')


async def test_video_processing_failure_is_terminal_and_never_creates_replacement(publisher,sessions):
    service,_,calls,_=publisher
    p=await prepare(service)
    with sessions.begin() as db:
        from app.database.models import Publication
        item=db.get(Publication,p['publication_id']);payload=dict(item.payload);payload['request']=dict(payload['request'],content_type='video')
        from app.services.publishing import content_hash
        item.payload=payload;item.request_hash=content_hash(payload)
        t=db.get(PublicationTarget,p['targets'][0]['target_id']);t.state='processing';t.checkpoint={'stage':'video_processing','remote_id':'video_id'};t.attempts=1
    def handler(r):
        assert r.method=='GET'
        return httpx.Response(200,json={'id':'video_id','status':{'video_status':'error'}})
    service.connector_factory=lambda a,e:MetaPublishingConnector(FacebookConnector(GraphClient('synthetic','v26.0',transport=httpx.MockTransport(handler)),a.remote_id))
    result=await service.refresh_status(p['publication_id'],'alice')
    assert result['state']=='failed' and result['targets'][0]['error']['code']=='media_processing_failed'
    await service.refresh_status(p['publication_id'],'alice');assert not calls
