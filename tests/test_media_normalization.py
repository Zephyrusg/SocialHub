"""No encoder in normal tests: signed worker responses use tiny pregenerated MP4s."""
import hashlib
import asyncio
from pathlib import Path
import shutil

import httpx
import pytest
from sqlalchemy import select, func

from app.core import HubError
from app.database.models import Audit, Idempotency, Media, Publication
from app.services.feature_flags import FeatureFlags
from app.services.media_normalization import MediaNormalization
from app.services.normalization_protocol import PROFILE, WORKER_URL, request_signature, response_signature
from test_administration import config
from test_attachment_ingestion import Chunks

pytestmark = pytest.mark.skipif(not shutil.which('ffprobe'), reason='Prebuilt ffprobe required')
SECRET = 'synthetic-worker-secret-' * 4
SOURCE = Path('tests/fixtures/reel-h264-aac-edit-list-5s.mp4')
OUTPUT = Path('tests/fixtures/reel-h264-aac-5s.mp4').read_bytes()


def service(sessions, tmp_path, *, activate=True, transport=None):
    settings = config(sessions, admin_allowed_subjects=['owner-sub'],
        media_root=str(tmp_path / 'media'), media_ingestion_enabled=True,
        media_normalization_enabled=True, media_normalization_secret=SECRET)
    result = MediaNormalization(settings, sessions, transport)
    if activate:FeatureFlags(settings, sessions).save('owner-sub', 0, 'media_normalization', True)
    return result, settings


def worker(seen, *, change=None):
    async def handle(request):
        assert str(request.url) == WORKER_URL and request.method == 'POST'
        body = await request.aread();seen.append(body)
        h = request.headers
        assert h['x-signature'] == request_signature(SECRET, h['x-job'], h['x-expires'],
            hashlib.sha256(body).hexdigest(), len(body))
        digest = hashlib.sha256(OUTPUT).hexdigest()
        headers = {'Content-Type': 'video/mp4', 'Content-Length': str(len(OUTPUT)),
                   'X-Output-Sha256': digest,
                   'X-Signature': response_signature(SECRET, h['x-job'], digest, len(OUTPUT))}
        if change:headers.update(change)
        return httpx.Response(200, headers=headers, stream=Chunks(OUTPUT))
    return httpx.MockTransport(handle)


async def test_opt_in_defaults_and_runtime_disable_stop_existing_service(sessions, tmp_path):
    seen=[];subject,settings=service(sessions,tmp_path,activate=False,transport=worker(seen))
    flag=next(f for f in FeatureFlags(settings,sessions).status('owner-sub')['features'] if f['name']=='media_normalization')
    assert flag['available'] and flag['configured'] is None and not flag['effective']
    source=subject.manager.save(SOURCE,'owner-sub')
    with pytest.raises(HubError) as error:await subject.normalize(source['id'],'owner-sub','one')
    assert error.value.code=='media_normalization_disabled' and not seen
    flags=FeatureFlags(settings,sessions);flags.save('owner-sub',0,'media_normalization',True)
    flags.save('owner-sub',1,'media_import',False)
    with pytest.raises(HubError):await subject.normalize(source['id'],'owner-sub','one')
    assert not seen
    settings.media_normalization_enabled=False
    with pytest.raises(HubError):flags.save('owner-sub',2,'media_normalization',True)


async def test_fresh_derivative_reuse_provenance_and_no_publication(sessions,tmp_path):
    seen=[];subject,_=service(sessions,tmp_path,transport=worker(seen))
    source=subject.manager.save(SOURCE,'owner-sub');original=dict(source)
    assert source['metadata']['has_edit_list']
    response=await subject.normalize(source['id'],'owner-sub','same')
    assert not response['published'] and response['normalized'] and not response['reused']
    assert response['media']['id']!=source['id'] and response['media']['sha256']!=source['sha256']
    assert response['media']['metadata']['has_edit_list'] is False
    assert response['media']['metadata']['normalization']['profile']==PROFILE
    assert subject.manager.get(source['id'],'owner-sub')==original
    assert (Path(subject.settings.media_root)/source['id']).read_bytes()==SOURCE.read_bytes()
    again=await subject.normalize(source['id'],'owner-sub','same')
    assert again['reused'] and again['media']['id']==response['media']['id'] and len(seen)==1
    assert not list(Path(subject.settings.media_root).glob('.upload-*'))
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Publication))==0
        assert db.scalar(select(func.count()).select_from(Media))==2
        audit=db.scalar(select(Audit).where(Audit.operation=='normalize_media'))
        assert audit.subject=='owner-sub' and audit.details['source_media_id']==source['id']
        assert audit.details['media_id']==response['media']['id']
    flags=FeatureFlags(subject.settings,sessions);flags.save('owner-sub',1,'media_normalization',False)
    with pytest.raises(HubError):await subject.normalize(source['id'],'owner-sub','same')
    assert len(seen)==1


async def test_owned_assets_and_idempotency_binding(sessions,tmp_path):
    seen=[];subject,_=service(sessions,tmp_path,transport=worker(seen))
    source=subject.manager.save(SOURCE,'owner-sub')
    with pytest.raises(HubError):await subject.normalize(source['id'],'outsider','key')
    assert not seen
    response=await subject.normalize(source['id'],'owner-sub','key')
    with pytest.raises(HubError) as error:await subject.normalize(response['media']['id'],'owner-sub','key')
    assert error.value.code=='idempotency_conflict' and len(seen)==1


async def test_valid_input_does_not_reencode_or_call_worker(sessions,tmp_path):
    subject,_=service(sessions,tmp_path,transport=httpx.MockTransport(lambda r:pytest.fail('unnecessary conversion')))
    source=subject.manager.save(Path('tests/fixtures/reel-h264-aac-5s.mp4'),'owner-sub')
    result=await subject.normalize(source['id'],'owner-sub','valid')
    assert result['already_compatible'] and not result['normalized'] and result['media']['id']==source['id']
    assert (await subject.normalize(source['id'],'owner-sub','valid'))['reused']


@pytest.mark.parametrize('change',[{'X-Signature':'0'*64},{'X-Output-Sha256':'0'*64},
    {'Content-Length':str(101*1024*1024)},{'Content-Encoding':'gzip'}, {'Content-Type':'text/html'}])
async def test_untrusted_worker_response_registers_nothing(sessions,tmp_path,change):
    subject,_=service(sessions,tmp_path,transport=worker([],change=change))
    source=subject.manager.save(SOURCE,'owner-sub')
    with pytest.raises(HubError) as error:await subject.normalize(source['id'],'owner-sub','invalid')
    assert error.value.code=='media_normalization_unavailable'
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Media))==1
        assert db.scalar(select(func.count()).select_from(Idempotency))==0
    assert not list(Path(subject.settings.media_root).glob('.upload-*'))


async def test_toggle_disabled_while_encoding_discards_derivative(sessions,tmp_path):
    seen=[];subject,settings=service(sessions,tmp_path)
    async def handler(request):
        FeatureFlags(settings,sessions).save('owner-sub',1,'media_normalization',False)
        return await worker(seen).handle_async_request(request)
    subject.transport=httpx.MockTransport(handler)
    source=subject.manager.save(SOURCE,'owner-sub')
    with pytest.raises(HubError) as error:await subject.normalize(source['id'],'owner-sub','cancelled')
    assert error.value.code=='media_normalization_disabled'
    with sessions() as db:assert db.scalar(select(func.count()).select_from(Media))==1


async def test_authenticated_bad_video_is_independently_rejected(sessions,tmp_path,monkeypatch):
    seen=[];subject,_=service(sessions,tmp_path,transport=worker(seen))
    source=subject.manager.save(SOURCE,'owner-sub')
    monkeypatch.setattr('app.services.media_normalization.video_fingerprint',lambda p: {'packets':1,'sha256':str(p)})
    with pytest.raises(HubError) as error:await subject.normalize(source['id'],'owner-sub','changed-video')
    assert error.value.code=='invalid_media'
    with sessions() as db:assert db.scalar(select(func.count()).select_from(Media))==1


async def test_mcp_conversion_requires_scope_and_existing_publisher_role(sessions,monkeypatch):
    from types import SimpleNamespace
    from app.main import create_server
    settings=config(sessions, auth_mode='oauth', admin_allowed_subjects=['owner-sub'])
    tool=create_server(settings)._tool_manager.get_tool('normalize_media')
    async def call(token):
        monkeypatch.setattr('app.security.attachment_tools.get_access_token',lambda:token)
        return await tool.fn(media_id='private-id',idempotency_key='key')
    assert (await call(None))['error']['code']=='unauthenticated'
    assert (await call(SimpleNamespace(client_id='owner-sub',scopes=['socialhub:read'])))['error']['code']=='insufficient_scope'
    assert (await call(SimpleNamespace(client_id='wife-sub',scopes=['socialhub:read','socialhub:publish'])))['error']['code']=='publishing_role_required'
    assert (await call(SimpleNamespace(client_id='owner-sub',scopes=['socialhub:read','socialhub:publish'])))['error']['code']=='media_normalization_disabled'


async def test_concurrent_calls_cannot_start_duplicate_jobs(sessions,tmp_path):
    started=asyncio.Event();release=asyncio.Event();seen=[]
    async def handle(request):
        started.set();await release.wait()
        return await worker(seen).handle_async_request(request)
    subject,_=service(sessions,tmp_path,transport=httpx.MockTransport(handle))
    source=subject.manager.save(SOURCE,'owner-sub')
    first=asyncio.create_task(subject.normalize(source['id'],'owner-sub','one-job'))
    await asyncio.wait_for(started.wait(),3)
    with pytest.raises(HubError) as error:await subject.normalize(source['id'],'owner-sub','one-job')
    assert error.value.code=='media_busy'
    release.set();result=await first
    assert (await subject.normalize(source['id'],'owner-sub','one-job'))['media']['id']==result['media']['id']
    assert len(seen)==1


async def test_cancelled_transfer_cleans_staging_and_registers_no_result(sessions,tmp_path):
    started=asyncio.Event()
    async def handle(request):
        started.set();await asyncio.Event().wait()
    subject,_=service(sessions,tmp_path,transport=httpx.MockTransport(handle))
    source=subject.manager.save(SOURCE,'owner-sub')
    task=asyncio.create_task(subject.normalize(source['id'],'owner-sub','cancel'))
    await asyncio.wait_for(started.wait(),3);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not list(Path(subject.settings.media_root).glob('.upload-*'))
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Media))==1
        assert db.scalar(select(func.count()).select_from(Idempotency))==0
