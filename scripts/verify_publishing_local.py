"""Safe Phase 2B integration check: private uploads/previews, denied execution, real reads only."""
import argparse
import asyncio
import hashlib
from io import BytesIO
import json
from pathlib import Path
from uuid import uuid4
import httpx
from PIL import Image
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from app.config import Settings


async def main(video=None):
    s=Settings()
    if s.meta_live_writes or s.publishing_enabled:
        raise SystemExit('This development check requires both publishing flags OFF')
    headers={'Authorization':'Bearer '+s.local_token.get_secret_value()}
    report={'public_writes_performed':0,'configured_live_writes':False,'configured_publishing_enabled':False}
    async with streamablehttp_client(s.public_url,headers=headers) as (read,write,_):
        async with ClientSession(read,write) as client:
            await client.initialize()
            names={t.name for t in (await client.list_tools()).tools}
            assert {'prepare_publication','execute_publication','get_publication_preview','get_publication_status','list_media','get_media'}<=names
            report['discovered_tool_count']=len(names)
            async def call(name,args):
                result=await client.call_tool(name,args)
                return result.structuredContent
            status=await call('get_system_status',{})
            assert status['ok'] and not status['data']['live_writes_enabled']
            async def posts(aid):
                result=await call('list_posts',{'account_id':aid,'limit':100})
                assert result['ok']
                return result['data']
            before={a:await posts(a) for a in ['pixelpapa-facebook','pixelpapa-instagram']}
            nonce=uuid4().hex
            text={'account_ids':['pixelpapa-facebook'],'text':'Phase 2B private preview only; never authorized for public publication','idempotency_key':'local-text-'+nonce}
            preview=(await call('prepare_publication',text))
            assert preview['ok'],preview
            first=preview['data']
            assert (await call('prepare_publication',text))['data']['publication_id']==first['publication_id']
            denied=await call('execute_publication',{'publication_id':first['publication_id']})
            assert not denied['ok'] and denied['error']['code']=='live_writes_disabled'
            report['text_preview_id']=first['publication_id']
            report['preview_idempotency']='passed'
            report['execution_guard']=denied['error']['code']
            assets=[]
            content=BytesIO((Path(__file__).resolve().parents[1]/'tests/fixtures/portrait.jpg').read_bytes())
            candidates=[('image',content.getvalue())]
            if video:
                candidates.append(('reel',Path(video).read_bytes()))
            async with httpx.AsyncClient(timeout=180,headers=headers) as http:
                for kind,data in candidates:
                    response=await http.post(s.public_url.removesuffix('/mcp')+'/media/upload',content=data,headers={'Content-Type':'application/octet-stream'})
                    assert response.status_code==200, response.text
                    asset=response.json()['media']
                    download_url=s.public_url.removesuffix('/mcp')+'/media/'+asset['id']
                    assert (await http.get(download_url)).content==data
                    async with httpx.AsyncClient(timeout=10) as anonymous:
                        assert (await anonymous.get(download_url)).status_code==403
                    prepared=await call('prepare_publication',{'account_ids':['pixelpapa-facebook','pixelpapa-instagram'],
                        'text':'Phase 2B private media preview only; no public write authorized','content_type':kind,'media_ids':[asset['id']], 'idempotency_key':'local-'+kind+'-'+nonce})
                    assert prepared['ok'],prepared
                    assert len(prepared['data']['targets'])==2 and prepared['data']['requires_owner_approval']
                    blocked=await call('execute_publication',{'publication_id':prepared['data']['publication_id']})
                    assert blocked['error']['code']=='live_writes_disabled'
                    assets.append({'content_type':kind,'media_id':asset['id'],'mime_type':asset['mime_type'],
                        'metadata':asset['metadata'],'size':asset['size'],'publication_id':prepared['data']['publication_id'], 'target_count':2})
            after={a:await posts(a) for a in before}
            assert before==after,'Observed content changed during read-only development verification'
            report.update(media=assets, existing_content_unchanged=True,
                facebook_post_count=len(after['pixelpapa-facebook']['items']), instagram_post_count=len(after['pixelpapa-instagram']['items']),
                status='passed')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--video',type=Path);a=p.parse_args()
    asyncio.run(main(a.video))
