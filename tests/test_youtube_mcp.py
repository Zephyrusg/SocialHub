"""The generic MCP preview exposes structured YouTube metadata without uploading."""
import json
from pathlib import Path
import httpx
from pydantic import SecretStr
from app.main import create_server
from app.services.media_manager import MediaManager
from app.connectors.youtube import YouTubeConnector,YouTubeClient
from app.services.youtube_oauth import access_token
from test_youtube import yt_settings,connected,ACCOUNT,META

async def test_youtube_mcp_preview_and_read_capabilities(yt_settings,sessions,monkeypatch):
    yt_settings.database_url=SecretStr(str(sessions.kw['bind'].url))
    google,_=await connected(yt_settings,sessions)
    async def client(self,writes=False):
        token=await access_token(self.settings,self.sessions,httpx.MockTransport(google))
        return YouTubeClient(token['access_token'],httpx.MockTransport(google),writes)
    monkeypatch.setattr(YouTubeConnector,'client',client)
    asset=MediaManager(yt_settings,sessions).save(Path('tests/fixtures/reel-h264-aac-5s.mp4'),'local-developer')
    app=create_server(yt_settings).streamable_http_app()
    headers={'Authorization':'Bearer '+'a'*64,'Accept':'application/json, text/event-stream'}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost:8787',headers=headers) as c:
            async def rpc(method,params):
                r=await c.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':method,'params':params});assert r.status_code==200
                return json.loads(next(l[6:] for l in r.text.splitlines() if l.startswith('data: ')))['result']
            await rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'pytest','version':'1'}})
            tools=(await rpc('tools/list',{}))['tools'];prepare=next(t for t in tools if t['name']=='prepare_publication')
            assert 'youtube' in prepare['inputSchema']['properties'] and prepare['annotations']['readOnlyHint'] is False
            assert len(tools)==21  # One image preparation tool; shared publication tools remain unchanged.
            r=(await rpc('tools/call',{'name':'prepare_publication','arguments':{'account_ids':[ACCOUNT],'content_type':'short',
                'media_ids':[asset['id']],'youtube':META,'idempotency_key':'mcp-youtube'}}))['structuredContent']
            assert r['ok'] and r['data']['state']=='prepared' and not r['data']['live_writes_enabled']
            assert r['data']['preview']['request']['youtube']['visibility']=='private'
            assert not any(m=='POST' and p!='/token' for m,p in google.calls)
