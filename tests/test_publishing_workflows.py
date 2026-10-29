import httpx
import pytest
from app.connectors.meta import GraphClient, FacebookConnector, InstagramConnector
from app.connectors.publishing import MetaPublishingConnector
from app.connectors.meta_policy import PAGE_ID, INSTAGRAM_ID
from app.core import HubError


def adapter(platform, handler):
    graph = GraphClient('synthetic', 'v26.0', transport=httpx.MockTransport(handler), allow_writes=True)
    cls, aid = (FacebookConnector, PAGE_ID) if platform == 'facebook' else (InstagramConnector, INSTAGRAM_ID)
    return MetaPublishingConnector(cls(graph, aid)), aid


def request(kind='text'):
    return {'operation': 'publish', 'content_type': kind, 'text': 'Previewed caption', 'link_url': 'https://example.com/link', 'media_url': 'https://media.example.test/media/asset?signature=synthetic'}


@pytest.mark.parametrize('kind,edge,field', [('text', 'feed','message'),('link','feed','link'),('image','photos','url'),('video','videos','file_url')])
async def test_distinct_facebook_endpoints_and_payloads(kind, edge, field):
    seen = []
    def handler(r):
        seen.append(r)
        return httpx.Response(200, json={'id': 'remote', **({'post_id': 'page_post'} if kind=='image' else {})})
    a, aid = adapter('facebook', handler)
    result = await a.step(aid, request(kind), {})
    assert seen[0].method == 'POST' and seen[0].url.path.endswith('/'+edge)
    assert (field+'=').encode() in seen[0].content
    assert result['state'] == 'processing'
    assert result['checkpoint']['remote_id'] == ('page_post' if kind=='image' else 'remote')


async def test_permalink_finalization_does_not_republish():
    seen=[]
    def handler(r):
        seen.append(r)
        assert r.method=='GET'
        return httpx.Response(200,json={'id':'remote','permalink_url':'https://www.facebook.com/remote'})
    a,aid=adapter('facebook',handler)
    result=await a.step(aid,request(),{'stage':'delivered','remote_id':'remote'})
    assert result['state']=='succeeded' and result['result']['permalink']=='https://www.facebook.com/remote'
    assert len(seen)==1


async def test_instagram_container_poll_publish_and_permalink():
    seen=[]
    def handler(r):
        seen.append(r)
        path=r.url.path.rsplit('/',1)[-1]
        if r.method=='POST' and path=='media': return httpx.Response(200,json={'id':'container'})
        if r.method=='GET' and path=='container': return httpx.Response(200,json={'status_code':'FINISHED'})
        if r.method=='POST' and path=='media_publish': return httpx.Response(200,json={'id':'published'})
        if r.method=='GET' and path=='published': return httpx.Response(200,json={'id':'published','permalink':'https://www.instagram.com/p/test/'})
        raise AssertionError('Unexpected request')
    a,aid=adapter('instagram',handler)
    first=await a.step(aid,request('image'),{})
    assert first['checkpoint']['container_id']=='container' and first['retry_after']==60
    second=await a.step(aid,request('image'),first['checkpoint'])
    third=await a.step(aid,request('image'),second['checkpoint'])
    final=await a.step(aid,request('image'),third['checkpoint'])
    assert final['state']=='succeeded'
    assert [r.url.path.rsplit('/',1)[-1] for r in seen if r.method=='POST']==['media','media_publish']


@pytest.mark.parametrize('status,code', [('IN_PROGRESS',None),('ERROR','media_processing_failed'),('EXPIRED','media_processing_failed'),('PUBLISHED','write_outcome_unknown')])
async def test_instagram_poll_states_never_recreate_container(status,code):
    def handler(r):
        assert r.method=='GET'
        return httpx.Response(200,json={'status_code':status})
    a,aid=adapter('instagram',handler)
    if code:
        with pytest.raises(HubError) as error:
            await a.step(aid,request('reel'),{'stage':'ig_container','container_id':'existing'})
        assert error.value.code==code
    else:
        result=await a.step(aid,request('reel'),{'stage':'ig_container','container_id':'existing'})
        assert result['state']=='processing' and result['retry_after']==60


async def test_poll_timeout_is_identified_as_read_only():
    def handler(r): raise httpx.ReadTimeout('sensitive')
    a,aid=adapter('instagram',handler)
    with pytest.raises(HubError) as error:
        await a.step(aid,request('reel'),{'stage':'ig_container','container_id':'existing'})
    assert error.value.retryable and error.value.diagnostics['mutation_attempted'] is False


async def test_facebook_reel_real_multistage_contract(tmp_path):
    seen=[]
    def handler(r):
        seen.append(r)
        if r.url.host=='rupload.facebook.com':
            assert 'file_url' not in r.headers
            assert r.content == b'synthetic-video'
            assert r.headers['Authorization']=='OAuth synthetic'
            return httpx.Response(200,json={'success':True})
        if r.method=='POST' and b'upload_phase=start' in r.content:
            return httpx.Response(200,json={'video_id':'123','upload_url':'https://rupload.facebook.com/video-upload/v26.0/123'})
        if r.method=='POST' and b'upload_phase=finish' in r.content:
            assert b'video_state=PUBLISHED' in r.content
            return httpx.Response(200,json={'success':True})
        return httpx.Response(200,json={'id':'123','status':{'video_status':'ready','uploading_phase':{'status':'complete'},'processing_phase':{'status':'complete'},'publishing_phase':{'status':'complete' if any(b'upload_phase=finish' in x.content for x in seen) else 'not_started'}},'permalink_url':'https://www.facebook.com/reel/123'})
    a,aid=adapter('facebook',handler)
    from hashlib import sha256
    path=tmp_path/'video'; path.write_bytes(b'synthetic-video')
    req=dict(request('reel'),_media_path=str(path),_media_size=15,_media_sha256=sha256(path.read_bytes()).hexdigest())
    checkpoint={}
    for _ in range(5):
        result=await a.step(aid,req,checkpoint)
        checkpoint=result['checkpoint']
    assert result['state']=='succeeded'
    assert sum(r.method=='POST' for r in seen)==3


async def test_untrusted_reel_upload_url_rejected_without_network():
    def handler(r):
        assert r.url.host=='graph.facebook.com'
        return httpx.Response(200,json={'video_id':'123','upload_url':'https://evil.example/video-upload/123'})
    a,aid=adapter('facebook',handler)
    with pytest.raises(HubError):
        await a.step(aid,request('reel'),{})


async def test_media_update_is_not_silently_replaced():
    def handler(r):
        assert r.method=='GET'
        if r.url.path.endswith('/posts'):
            return httpx.Response(200,json={'data':[{'id':'owned','message':'old'}]})
        return httpx.Response(200,json={'id':'owned','attachments':{'data':[{'type':'video'}]}})
    a,aid=adapter('facebook',handler)
    with pytest.raises(HubError) as error:
        await a.validate(aid,{'operation':'update','content_type':'text','text':'new','post_id':'owned'},[])
    assert error.value.code=='unsupported_operation'


async def test_instagram_text_link_and_edit_capabilities_are_refused():
    a,aid=adapter('instagram',lambda r: (_ for _ in ()).throw(AssertionError('No network expected')))
    for item in [request('text'),request('link'),dict(request(),operation='update')]:
        with pytest.raises(HubError): await a.validate(aid,item,[])


@pytest.mark.parametrize('width,height,duration',[(180,320,3),(720,1280,2),(1080,1920,61),(1280,720,10)])
async def test_reel_format_constraints_refused_before_network(width,height,duration):
    a,aid=adapter('instagram',lambda r: (_ for _ in ()).throw(AssertionError('No network')))
    asset={'mime_type':'video/mp4','size':1000,'metadata':{'width':width,'height':height,'duration':duration}}
    with pytest.raises(HubError) as error:await a.validate(aid,request('reel'),[asset])
    assert error.value.code=='invalid_media'
