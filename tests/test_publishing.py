import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from pathlib import Path
import httpx
from PIL import Image
import pytest
from sqlalchemy import select, func
from app.core import HubError
from app.database.models import Publication, PublicationTarget, PublicationApproval, Media, utcnow
from app.models_publication import PublicationRequest
from app.services.publishing import PublishingService
from app.services.media_manager import MediaManager
from app.services.meta_oauth import MetaOAuth
from app.connectors.meta import GraphClient, FacebookConnector, InstagramConnector
from app.connectors.publishing import MetaPublishingConnector
from app.connectors.meta_policy import PAGE_ID, INSTAGRAM_ID
from test_meta_live import MetaFixture


class FakeAdapter:
    def __init__(self, platform, calls):
        self.platform, self.calls = platform, calls
        self.fail = None
        self.delay = False

    async def validate(self, account_id, request, assets):
        if self.platform == 'instagram' and request['content_type'] == 'text' and request['operation'] == 'publish':
            raise HubError('unsupported_operation', 'Image/Reel required')

    async def step(self, account_id, request, checkpoint):
        self.calls.append((self.platform, dict(checkpoint)))
        if self.delay:
            await asyncio.sleep(0.05)
        if self.fail:
            raise self.fail
        if checkpoint:
            return {'state': 'succeeded', 'checkpoint': checkpoint, 'result': {'remote_id': self.platform + '_remote', 'permalink': 'https://example.test/post'}}
        return {'state': 'processing', 'checkpoint': {'stage': 'delivered', 'remote_id': self.platform + '_remote'}}


@pytest.fixture
async def publisher(sessions, tmp_path):
    from app.config import Settings
    from cryptography.fernet import Fernet
    s = Settings(_env_file=None, database_url='sqlite://', encryption_key=Fernet.generate_key().decode(),
        local_token='a'*64, meta_app_id='12345', meta_app_secret='fake-app-secret', meta_live_reads=True,
        publishing_enabled=True, meta_live_writes=True,meta_write_account_ids=["pixelpapa-facebook","pixelpapa-instagram"], media_root=str(tmp_path / 'media'), media_public_base_url='https://media.example.test')
    await MetaOAuth(s, sessions, httpx.MockTransport(MetaFixture())).connect('long-user')
    calls = []
    adapters = {p: FakeAdapter(p, calls) for p in ['facebook', 'instagram']}
    now = [utcnow()]
    service = PublishingService(s, sessions, lambda account, execute: adapters[account.platform], clock=lambda: now[0])
    return service, adapters, calls, now


def image_asset(service, tmp_path, principal='alice', fmt='JPEG'):
    path = tmp_path / 'source-image'
    path.write_bytes(Path('tests/fixtures/' + ('portrait.jpg' if fmt == 'JPEG' else 'portrait.png')).read_bytes())
    return service.media.save(path, principal)


async def prepare(service, key='one', principal='alice', accounts=None, **kwargs):
    return await service.prepare(principal, key, PublicationRequest(account_ids=accounts or ['pixelpapa-facebook'], text='Explicit preview caption', **kwargs))


def approve(service, preview):
    return service.approve(preview['publication_id'], preview['request_hash'], 'test-owner', 'Publish this exact content to these accounts')


async def test_preview_is_durable_and_has_no_external_effect(publisher, sessions):
    service, _, calls, _ = publisher
    preview = await prepare(service)
    assert preview['state'] == 'prepared' and preview['requires_owner_approval']
    assert calls == []
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(PublicationTarget)) == 1
        assert db.scalar(select(func.count()).select_from(PublicationApproval)) == 0


async def test_prepare_idempotency_and_conflict(publisher):
    service, _, _, _ = publisher
    a, b = await prepare(service), await prepare(service)
    assert a['publication_id'] == b['publication_id']
    with pytest.raises(HubError) as error:
        await service.prepare('alice', 'one', PublicationRequest(account_ids=['pixelpapa-facebook'], text='changed'))
    assert error.value.code == 'idempotency_conflict'


async def test_concurrent_prepare_unique(publisher, sessions):
    service, _, _, _ = publisher
    def run(_):
        return asyncio.run(prepare(service))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(8)))
    assert len({r['publication_id'] for r in results}) == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Publication)) == 1


async def test_approval_cannot_be_model_boolean_or_wrong_hash(publisher):
    service, _, calls, _ = publisher
    with pytest.raises(ValueError):
        PublicationRequest.model_validate({'account_ids': ['pixelpapa-facebook'], 'text': 'Hello', 'approved': True})
    preview = await prepare(service)
    with pytest.raises(HubError) as error:
        await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert error.value.code == 'authorization_required' and not calls
    with pytest.raises(HubError):
        service.approve(preview['publication_id'], 'wrong', 'owner', 'explicit user request')
    assert not calls


async def test_flag_scope_principal_and_expiry_gates(publisher):
    service, _, calls, now = publisher
    preview = await prepare(service)
    approve(service, preview)
    with pytest.raises(HubError):
        await service.execute('bob', ['socialhub:publish'], preview['publication_id'])
    with pytest.raises(HubError) as error:
        await service.execute('alice', ['socialhub:read'], preview['publication_id'])
    assert error.value.code == 'insufficient_scope'
    now[0] += timedelta(hours=2)
    with pytest.raises(HubError) as error:
        await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert error.value.code == 'authorization_required'
    service.settings.meta_live_writes = False
    with pytest.raises(HubError) as error:
        await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert error.value.code == 'live_writes_disabled' and not calls


async def test_checkpointed_execution_terminal_replay_no_duplicate(publisher):
    service, _, calls, _ = publisher
    preview = await prepare(service)
    approve(service, preview)
    pid = preview['publication_id']
    assert (await service.execute('alice', ['socialhub:publish'], pid))['state'] == 'in_progress'
    assert (await service.execute('alice', ['socialhub:publish'], pid))['state'] == 'succeeded'
    await service.execute('alice', ['socialhub:publish'], pid)
    assert len(calls) == 2
    assert sum(not checkpoint for _, checkpoint in calls) == 1


async def test_concurrent_execution_claim(publisher):
    service, adapters, calls, _ = publisher
    adapters['facebook'].delay = True
    preview = await prepare(service)
    approve(service, preview)
    await asyncio.gather(*(service.execute('alice', ['socialhub:publish'], preview['publication_id']) for _ in range(8)))
    assert len(calls) == 1


async def test_partial_failure_is_not_success(publisher, tmp_path):
    service, adapters, calls, _ = publisher
    asset = image_asset(service, tmp_path)
    preview = await prepare(service, accounts=['pixelpapa-facebook','pixelpapa-instagram'], content_type='image', media_ids=[asset['id']])
    approve(service, preview)
    adapters['instagram'].fail = HubError('permission_denied', 'Permission changed')
    first = await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert first['state'] == 'partial_failure'
    final = await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert final['state'] == 'partial_failure'
    assert {t['state'] for t in final['targets']} == {'succeeded','failed'}


async def test_uncertain_network_outcome_is_never_resent(publisher):
    service, adapters, calls, _ = publisher
    preview = await prepare(service)
    approve(service, preview)
    adapters['facebook'].fail = HubError('api_timeout', 'Unknown outcome', False)
    a = await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert a['state'] == 'attention_required'
    adapters['facebook'].fail = None
    await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert len(calls) == 1


async def test_expired_worker_lease_becomes_uncertain(publisher, sessions):
    service, _, calls, now = publisher
    preview = await prepare(service)
    approve(service, preview)
    tid = preview['targets'][0]['target_id']
    assert service.claim(preview['publication_id'], tid, 'alice')
    now[0] += timedelta(minutes=6)
    result = await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert result['state'] == 'attention_required' and not calls


async def test_rate_limit_backoff_and_no_immediate_retry(publisher):
    service, adapters, calls, now = publisher
    preview = await prepare(service)
    approve(service, preview)
    adapters['facebook'].fail = HubError('rate_limited', 'Try later')
    await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert len(calls) == 1
    now[0] += timedelta(seconds=61)
    adapters['facebook'].fail = None
    await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert len(calls) == 2


async def test_preview_tampering_denied(publisher, sessions):
    service, _, calls, _ = publisher
    preview = await prepare(service)
    approve(service, preview)
    with sessions.begin() as db:
        p = db.get(Publication, preview['publication_id'])
        p.payload = dict(p.payload, request=dict(p.payload['request'], text='changed after approval'))
    with pytest.raises(HubError) as error:
        await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert error.value.code == 'preview_changed' and not calls


async def test_media_bytes_changed_after_approval_prevents_send(publisher, tmp_path):
    service, _, calls, _ = publisher
    asset = image_asset(service, tmp_path)
    preview = await prepare(service, content_type='image', media_ids=[asset['id']])
    approve(service, preview)
    path, _ = service.media.path(asset['id'])
    path.write_bytes(b'changed bytes')
    result = await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert result['targets'][0]['error']['code'] == 'media_changed' and not calls


async def test_signed_media_delivery_is_explicitly_required(publisher, tmp_path):
    service, _, calls, _ = publisher
    asset = image_asset(service, tmp_path)
    preview = await prepare(service, content_type='image', media_ids=[asset['id']])
    approve(service, preview)
    service.settings.media_public_base_url = ''
    result = await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert result['targets'][0]['error']['code'] == 'media_delivery_unconfigured' and not calls


@pytest.mark.parametrize('kwargs', [
    {'account_ids': ['pixelpapa-facebook'], 'text': ''},
    {'account_ids': ['pixelpapa-facebook'], 'content_type': 'image'},
    {'account_ids': ['pixelpapa-facebook'], 'text':'x', 'operation':'reply', 'post_id':'p'},
    {'account_ids': ['pixelpapa-facebook']*2, 'text':'x'},
    {'account_ids': ['pixelpapa-facebook'], 'text':'x', 'link_url':'https://example.com'},
])
def test_request_validation(kwargs):
    with pytest.raises(ValueError):
        PublicationRequest(**kwargs)
