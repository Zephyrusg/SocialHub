from io import BytesIO
from pathlib import Path
import asyncio
import json
import socket
import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select,func
from app.config import Settings
from app.core import HubError
from app.database.models import Base,Media,Idempotency
from app.services.attachment_download import ChatGPTFile,AttachmentDownloader,attachment_host,public_address
from app.services.media_ingestion import MediaIngestion

HOST='files.example.test'
SECRET='CAPABILITY_MUST_NOT_APPEAR_IN_ERRORS'
JPEG=Path('tests/fixtures/portrait.jpg').read_bytes()


@pytest.fixture
def media_settings(tmp_path):
    return Settings(_env_file=None,database_url='sqlite://',encryption_key=Fernet.generate_key().decode(),
        local_token='a'*64,media_root=str(tmp_path/'media'),media_ingestion_enabled=True,
        media_attachment_probe_enabled=True,media_download_hosts=[HOST])


def reference(**changes):
    return ChatGPTFile(download_url=f'https://{HOST}/file?signature={SECRET}',file_id='file-test',**changes)


class Chunks(httpx.AsyncByteStream):
    def __init__(self,body):self.body=body
    async def __aiter__(self):
        for pos in range(0,len(self.body),1234):yield self.body[pos:pos+1234]


def downloader(settings,seen,body=JPEG,headers=None,status=200):
    async def resolver(host):
        assert host==HOST
        return '8.8.8.8'
    def handler(r):
        seen.append(r)
        assert r.method=='GET' and r.url.host=='8.8.8.8'
        assert r.headers['host']==HOST and r.extensions['sni_hostname']==HOST
        assert 'authorization' not in r.headers and 'cookie' not in r.headers
        return httpx.Response(status,headers=headers,stream=Chunks(body))
    return AttachmentDownloader(settings,httpx.MockTransport(handler),resolver)


@pytest.mark.parametrize('url', ['http://files.example.test/x','file:///etc/passwd',
    'https://user:password@files.example.test/x','https://files.example.test:444/x',
    'https://127.0.0.1/x','https://[::1]/x','https://files.example.test/x#fragment',
    'https://files.example.test./x','https://files.example.test/x\nignored'])
def test_invalid_reference_never_echoes_url(url):
    ref=ChatGPTFile(download_url=url+'?'+SECRET,file_id='file-test')
    with pytest.raises(HubError) as error:attachment_host(ref)
    assert SECRET not in str(error.value)


async def test_import_streams_pins_tls_deduplicates_and_does_not_publish(media_settings,sessions):
    seen=[];service=MediaIngestion(media_settings,sessions,downloader(media_settings,seen))
    first=await service.import_file(reference(),'alice','one')
    assert first['media']['mime_type']=='image/jpeg' and first['suggested_content_type']=='image'
    assert first['expires_at'] and not first['published'] and len(seen)==1
    fresh=reference();fresh.download_url='https://expired.example.invalid/no-longer-needed'
    again=await service.import_file(fresh,'alice','one')
    assert again['reused'] and again['media']==first['media'] and len(seen)==1
    assert SECRET not in json.dumps(again)
    assert not list(Path(media_settings.media_root).glob('.upload-*'))
    assert (Path(media_settings.media_root)/first['media']['id']).stat().st_mode&0o777==0o600
    changed=reference();changed.file_id='file-other'
    with pytest.raises(HubError) as error:await service.import_file(changed,'alice','one')
    assert error.value.code=='idempotency_conflict'
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Media))==1
        assert db.scalar(select(func.count()).select_from(Idempotency))==1
        assert SECRET not in json.dumps(db.scalar(select(Idempotency)).response)


async def test_reference_only_probe_does_not_fetch_or_write(media_settings,sessions):
    media_settings.media_attachment_probe_enabled=False
    service=MediaIngestion(media_settings,sessions,downloader(media_settings,[]))
    result=await service.probe(reference())
    assert result['mode']=='reference_only' and not result['download_verified'] and not result['stored']
    assert result['download_host']==HOST and not Path(media_settings.media_root).exists()
    assert SECRET not in json.dumps(result) and 'file-test' not in json.dumps(result)


async def test_validated_probe_removes_every_temporary_byte(media_settings,sessions):
    seen=[];service=MediaIngestion(media_settings,sessions,downloader(media_settings,seen))
    result=await service.probe(reference())
    assert result['download_verified'] and result['mime_type']=='image/jpeg' and not result['stored']
    assert len(seen)==1 and not list(Path(media_settings.media_root).glob('.upload-*'))
    with sessions() as db:assert db.scalar(select(func.count()).select_from(Media))==0


async def test_exact_host_allowlist_no_redirects_no_private_destinations(media_settings):
    ref=reference();ref.download_url='https://files.example.test.evil.invalid/x'
    client=downloader(media_settings,[])
    with pytest.raises(HubError) as error:await client.receive(ref,BytesIO())
    assert error.value.code=='attachment_host_unverified'
    for ip in ['127.0.0.1','10.0.0.1','169.254.169.254','::1','fd00::1','224.0.0.1','ff02::1']:
        async def resolver(host):return ip
        client=AttachmentDownloader(media_settings,httpx.MockTransport(lambda r:pytest.fail('SSRF')),resolver)
        with pytest.raises(HubError):await client.receive(reference(),BytesIO())
    seen=[];client=downloader(media_settings,seen,status=302,headers={'location':'http://127.0.0.1/private'})
    with pytest.raises(HubError):await client.receive(reference(),BytesIO())
    assert len(seen)==1


async def test_dns_rejects_mixed_public_private_answers(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443)),(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    with pytest.raises(HubError):await public_address(HOST)


@pytest.mark.parametrize('headers,body',[({'content-length':'999999999'},JPEG),
    ({'content-length':'-1'},JPEG),({'content-length':'0'},b''),
    ({'content-encoding':'gzip'},JPEG),({'content-length':'1'},JPEG)])
async def test_bounds_reject_bad_transfer_headers(media_settings,headers,body):
    client=downloader(media_settings,[],body=body,headers=headers)
    with pytest.raises(HubError):await client.receive(reference(),BytesIO())


async def test_missing_length_still_cannot_exceed_stream_limit(media_settings,sessions):
    media_settings.media_max_bytes=100
    service=MediaIngestion(media_settings,sessions,downloader(media_settings,[],body=b'x'*101))
    with pytest.raises(HubError) as error:await service.import_file(reference(),'alice','too-big')
    assert error.value.code=='invalid_media' and not list(Path(media_settings.media_root).glob('.upload-*'))


async def test_cancelled_transfer_releases_lock_and_removes_partial_file(media_settings,sessions):
    started=asyncio.Event()
    class Slow:
        async def receive(self,ref,out):
            out.write(b'partial');started.set();await asyncio.Event().wait()
    service=MediaIngestion(media_settings,sessions,Slow())
    task=asyncio.create_task(service.import_file(reference(),'alice','cancel'))
    await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not list(Path(media_settings.media_root).glob('.upload-*'))
    with service.budget.upload() as path:assert path.exists()


async def test_probe_and_import_admission_before_second_file_allocation(media_settings,sessions):
    started=asyncio.Event();finish=asyncio.Event()
    class Slow:
        async def receive(self,ref,out):
            started.set();await finish.wait();out.write(JPEG)
    service=MediaIngestion(media_settings,sessions,Slow())
    first=asyncio.create_task(service.import_file(reference(),'alice','one'))
    await started.wait()
    with pytest.raises(HubError) as error:await service.import_file(reference(),'alice','two')
    assert error.value.code=='media_busy' and len(list(Path(media_settings.media_root).glob('.upload-*')))==1
    finish.set();await first


async def test_import_mp4_uses_existing_fixture_and_no_encoding(media_settings,sessions):
    import shutil
    if not shutil.which('ffprobe'):pytest.skip('ffprobe runs in the production/test images')
    body=Path('tests/fixtures/reel-h264-3s.mp4').read_bytes()
    service=MediaIngestion(media_settings,sessions,downloader(media_settings,[],body=body))
    result=await service.import_file(reference(mime_type='video/mp4',file_name='flow.mp4'),'alice','clip')
    assert result['suggested_content_type']=='video' and result['media']['metadata']['video_codec']=='h264'
    assert result['media']['sha256']==service.manager.file_hash(Path(media_settings.media_root)/result['media']['id'])


async def test_file_schema_and_sensitive_validation_errors_through_mcp(tmp_path,caplog):
    from app.database import database
    from app.main import create_server
    url=f'sqlite:///{tmp_path}/mcp.db';engine,_=database(url);Base.metadata.create_all(engine)
    s=Settings(_env_file=None,database_url=url,encryption_key=Fernet.generate_key().decode(),local_token='a'*64,media_root=str(tmp_path/'media'))
    app=create_server(s).streamable_http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost:8787',headers={'Authorization':'Bearer '+'a'*64,'Accept':'application/json, text/event-stream'}) as client:
            async def rpc(method,params):
                r=await client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
                assert r.status_code==200
                return json.loads(next(l[6:] for l in r.text.splitlines() if l.startswith('data: ')))['result']
            await rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'pytest','version':'1'}})
            tools=(await rpc('tools/list',{}))['tools']
            for name in ['import_media','probe_media_attachment']:
                tool=next(t for t in tools if t['name']==name)
                assert tool['_meta']['openai/fileParams']==['file']
                schema=tool['inputSchema'];ref=schema['properties']['file']['$ref'].split('/')[-1];file=schema['$defs'][ref]
                assert set(file['properties'])=={'download_url','file_id','mime_type','file_name'}
                assert set(file['required'])=={'download_url','file_id'}
                assert all(v['type']=='string' for v in file['properties'].values())
            result=await rpc('tools/call',{'name':'probe_media_attachment','arguments':{'file':reference().model_dump()}})
            assert result['structuredContent']['data']['mode']=='reference_only'
            bad=await rpc('tools/call',{'name':'probe_media_attachment','arguments':{'file':{'download_url':reference().download_url,'file_id':42}}})
            assert bad.get('isError') and SECRET not in json.dumps(bad) and SECRET not in caplog.text
            assert SECRET not in json.dumps(result)


async def test_download_deadline_cannot_be_kept_alive_by_slow_chunks(media_settings,sessions):
    media_settings.media_receive_timeout_seconds=1
    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'x'
            await asyncio.Event().wait()
    async def resolver(host):return '8.8.8.8'
    def handler(r):return httpx.Response(200,stream=Drip())
    client=AttachmentDownloader(media_settings,httpx.MockTransport(handler),resolver)
    service=MediaIngestion(media_settings,sessions,client)
    with pytest.raises(HubError) as error:await service.import_file(reference(),'alice','slow')
    assert error.value.code=='attachment_download_failed'
    assert not list(Path(media_settings.media_root).glob('.upload-*'))
