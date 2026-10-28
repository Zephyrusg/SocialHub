import httpx
import pytest
from app.connectors.meta import GraphClient,FacebookConnector
from app.connectors.publishing import facebook_permalink,MetaPublishingConnector


@pytest.mark.parametrize('value,expected',[
    ('/reel/123/','https://www.facebook.com/reel/123/'),
    ('https://www.facebook.com/page/posts/123','https://www.facebook.com/page/posts/123'),
    ('http://m.facebook.com/watch/?v=123','https://www.facebook.com/watch/?v=123'),
    (None,None),('',None),('//evil.invalid/x',None),('https://evil.invalid/reel/123',None),
    ('https://www.facebook.com.evil.invalid/x',None),('https://user@www.facebook.com/x',None),
    ('https://www.facebook.com:444/x',None),('/reel/123/\nsecret',None),('/\\evil.invalid',None)])
def test_full_facebook_permalink_only(value,expected):
    assert facebook_permalink(value)==expected


@pytest.mark.parametrize('checkpoint,kind',[
    ({'stage':'video_processing','remote_id':'123'},'video'),
    ({'stage':'delivered','remote_id':'123'},'text')])
async def test_meta_status_returns_absolute_reel_url_without_a_mutation(checkpoint,kind):
    calls=[]
    def handler(r):
        assert r.method=='GET';calls.append(r.method)
        return httpx.Response(200,json={'id':'123','status':{'video_status':'ready'},'permalink_url':'/reel/123/'})
    a=MetaPublishingConnector(FacebookConnector(GraphClient('synthetic','v26.0',transport=httpx.MockTransport(handler)),'page'))
    result=await a.refresh_delivery('page',{'operation':'publish','content_type':kind},checkpoint)
    assert result['state']=='succeeded' and result['result']['permalink']=='https://www.facebook.com/reel/123/' and calls==['GET']
