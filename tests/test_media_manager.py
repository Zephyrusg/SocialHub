from io import BytesIO
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import httpx
from PIL import Image
import pytest
from cryptography.fernet import Fernet
from app.config import Settings
from app.core import HubError
from app.services.media_manager import MediaManager
from app.database.models import Media


@pytest.fixture
def media(sessions,tmp_path):
    s=Settings(_env_file=None,database_url='sqlite://',local_token='a'*64,encryption_key=Fernet.generate_key().decode(),media_root=str(tmp_path/'assets'),media_public_base_url='https://media.example.test')
    return MediaManager(s,sessions)


def valid_image(path,fmt='JPEG'):
    path.write_bytes(Path('tests/fixtures/' + ('portrait.jpg' if fmt == 'JPEG' else 'portrait.png')).read_bytes())
    return path


@pytest.mark.parametrize('fmt',['JPEG','PNG'])
def test_decoded_image_and_principal_scoped_dedup(media,tmp_path,fmt):
    path=valid_image(tmp_path/'image',fmt)
    a=media.save(path,'alice')
    assert a['mime_type']==('image/jpeg' if fmt=='JPEG' else 'image/png') and a['metadata']['width']==800
    with pytest.raises(HubError):media.get(a['id'],'bob')
    b=media.save(path,'bob')
    assert a['id']==b['id']
    assert media.get(a['id'],'alice')['id']==media.get(a['id'],'bob')['id']
    assert 'owners' not in b['metadata']
    assert media.path(a['id'])[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('content',[b'\x89PNG\r\n\x1a\ninvalid',b'\xff\xd8\xffbad',b'not media',b''])
def test_fake_headers_and_invalid_bytes_refused(media,tmp_path,content):
    path=tmp_path/'bad';path.write_bytes(content)
    with pytest.raises(HubError):media.save(path,'alice')


def test_signed_urls_private_default_and_expiry(media,tmp_path,monkeypatch):
    asset=media.save(valid_image(tmp_path/'img'),'alice')
    monkeypatch.setattr('app.services.media_manager.time.time',lambda:1000)
    url=media.temporary_url(asset['id'],asset['sha256'])
    query=parse_qs(urlparse(url).query)
    assert media.verify_signature(asset['id'],query['expires'][0],query['signature'][0])[1]=='image/jpeg'
    with pytest.raises(HubError):media.verify_signature(asset['id'],query['expires'][0],'wrong')
    with pytest.raises(HubError):media.verify_signature('another-asset',query['expires'][0],query['signature'][0])
    monkeypatch.setattr('app.services.media_manager.time.time',lambda:2000)
    with pytest.raises(HubError):media.verify_signature(asset['id'],query['expires'][0],query['signature'][0])
    media.settings.media_public_base_url=''
    with pytest.raises(HubError):media.temporary_url(asset['id'],asset['sha256'])


def test_symlink_and_tampering_refused(media,tmp_path,sessions):
    asset=media.save(valid_image(tmp_path/'img'),'alice')
    path,_=media.path(asset['id']);path.unlink();path.symlink_to(tmp_path/'img')
    with pytest.raises(HubError):media.path(asset['id'])


@pytest.mark.skipif(not shutil.which('ffprobe'),reason='Prebuilt ffprobe validation runs in the Docker test target')
def test_actual_mp4_probe(media,tmp_path):
    video=tmp_path/'video.mp4'
    video.write_bytes(Path('tests/fixtures/reel-h264-3s.mp4').read_bytes())
    asset=media.save(video,'alice')
    assert asset['mime_type']=='video/mp4' and asset['metadata']['video_codec']=='h264'
    assert 2.9<=asset['metadata']['duration']<=3.1


async def test_authenticated_stream_upload_and_private_download(tmp_path):
    from app.database import database
    from app.database.models import Base
    from app.main import create_server
    url=f'sqlite:///{tmp_path}/http.db'
    engine,_=database(url);Base.metadata.create_all(engine)
    s=Settings(_env_file=None,database_url=url,local_token='a'*64,encryption_key=Fernet.generate_key().decode(),media_root=str(tmp_path/'media'),media_ingestion_enabled=True)
    app=create_server(s).streamable_http_app()
    content=BytesIO(Path('tests/fixtures/portrait.jpg').read_bytes())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost:8787') as client:
            assert (await client.post('/media/upload',content=content.getvalue())).status_code==403
            headers={'Authorization':'Bearer '+'a'*64}
            response=await client.post('/media/upload',content=content.getvalue(),headers=headers)
            assert response.status_code==200,response.text
            mid=response.json()['media']['id']
            assert (await client.get('/media/'+mid)).status_code==403
            download=await client.get('/media/'+mid,headers=headers)
            assert download.status_code==200 and download.content==content.getvalue()
            assert 'no-store' in download.headers['cache-control']
            assert not list((tmp_path/'media').glob('.upload-*'))


async def test_signed_download_range_and_no_cross_principal_access(tmp_path):
    from app.database import database
    from app.database.models import Base
    from app.main import create_server
    url=f'sqlite:///{tmp_path}/signed.db';engine,sessions=database(url);Base.metadata.create_all(engine)
    s=Settings(_env_file=None,database_url=url,local_token='a'*64,encryption_key=Fernet.generate_key().decode(),media_root=str(tmp_path/'media'),media_public_base_url='https://localhost:8787')
    manager=MediaManager(s,sessions)
    asset=manager.save(valid_image(tmp_path/'image'),'owner')
    signed=manager.temporary_url(asset['id'],asset['sha256'])
    app=create_server(s).streamable_http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            response=await client.get(signed,headers={'Range':'bytes=0-15'})
            assert response.status_code==206 and len(response.content)==16
            assert (await client.get(signed,headers={'Authorization':'Bearer '+'a'*64})).status_code==403
