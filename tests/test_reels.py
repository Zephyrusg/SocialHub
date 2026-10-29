"""Native binary uploads and durable multi-platform reconciliation; no real Meta calls."""
from collections import Counter
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
import struct
import shutil
import httpx
import pytest
from sqlalchemy import select, func
from app.core import HubError
from app.database.models import PublicationTarget, Publication, Post, Audit
from app.connectors.meta_policy import PAGE_ID, INSTAGRAM_ID
from app.services.mp4_layout import mp4_layout
from test_publishing import publisher, approve
from test_publishing_workflows import adapter, request

pytestmark = pytest.mark.skipif(not shutil.which('ffprobe'), reason='Reel metadata checks run in the Docker test target with prebuilt ffprobe')


class ReelMeta:
    def __init__(self):
        self.calls = Counter()
        self.uploaded, self.published = set(), set()
        self.fail_upload = None
        self.fail_publish = None
        self.processing = False
        self.upload_receiving = False
        self.reject_publish = None
        self.pending_publication = False
        self.permalink_ready = True

    def __call__(self, r):
        platform = 'facebook' if '/video-upload/' in r.url.path or '101' in r.url.path or PAGE_ID in r.url.path else 'instagram'
        if r.url.host == 'rupload.facebook.com':
            self.calls[platform + ':upload'] += 1
            assert r.headers['offset'] == '0'
            assert int(r.headers['file_size']) == len(r.content) == int(r.headers['content-length'])
            assert 'file_url' not in r.headers and not r.url.query
            assert r.content == Path('tests/fixtures/reel-h264-aac-5s.mp4').read_bytes()
            self.uploaded.add(platform)
            if self.fail_upload == platform:
                raise httpx.ReadTimeout('provider exception containing a pretend secret')
            return httpx.Response(200, json={'success': True})
        if r.method == 'POST':
            is_start = b'upload_phase=start' in r.content or r.url.path.endswith('/media')
            self.calls[platform + (':start' if is_start else ':publish')] += 1
            if is_start:
                if platform == 'facebook':
                    return httpx.Response(200, json={'video_id': '101', 'upload_url': 'https://rupload.facebook.com/video-upload/v26.0/101'})
                assert b'upload_type=resumable' in r.content and b'media_type=REELS' in r.content
                assert b'video_url' not in r.content
                return httpx.Response(200, json={'id': '202', 'uri': 'https://rupload.facebook.com/ig-api-upload/v26.0/202'})
            if self.reject_publish == platform:
                return httpx.Response(400, json={'error': {'code': 200, 'message': 'Permission denied'}})
            self.published.add(platform)
            if self.fail_publish == platform:
                raise httpx.ReadTimeout('pretend secret')
            return httpx.Response(200, json={'success': True} if platform == 'facebook' else {'id': '303'})
        self.calls[platform + ':read'] += 1
        if r.url.path.endswith('/303'):
            return httpx.Response(200, json={'id': '303', 'permalink': 'https://www.instagram.com/reel/example/'})
        if platform == 'instagram':
            return httpx.Response(200, json={'status_code': 'PUBLISHED' if platform in self.published else 'FINISHED' if platform in self.uploaded and not self.processing else 'IN_PROGRESS'})
        status = {'uploading_phase': {'status': 'complete' if platform in self.uploaded and not self.upload_receiving else 'in_progress'},
                  # Facebook finish initiates processing; it cannot complete beforehand.
                  'processing_phase': {'status': 'not_started' if platform not in self.published else 'in_progress' if self.processing else 'complete'},
                  'publishing_phase': {'status': 'complete' if platform in self.published and not self.pending_publication and not self.processing else 'in_progress' if platform in self.published else 'not_started'}}
        return httpx.Response(200, json={'id': '101', 'status': status,
            'permalink_url': '/reel/101/' if self.permalink_ready else None})


async def setup_reels(publisher, accounts=None, *, caption='Exact Reel caption'):
    service, _, _, now = publisher
    meta = ReelMeta()
    adapters = {p: adapter(p, meta)[0] for p in ('facebook', 'instagram')}
    service.connector_factory = lambda account, execute: adapters[account.platform]
    service.settings.facebook_media_allowed_types = ['reel']
    service.settings.media_delivery_enabled = False
    service.settings.media_public_base_url = ''
    asset = service.media.save(Path('tests/fixtures/reel-h264-aac-5s.mp4'), 'alice')
    preview = await service.prepare('alice', 'reel-one', {'account_ids': accounts or ['pixelpapa-facebook', 'pixelpapa-instagram'],
        'content_type': 'reel', 'text': caption, 'media_ids': [asset['id']]})
    approve(service, preview)
    async def execute():
        return await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    async def poll():
        now[0] += timedelta(seconds=61)
        return await service.refresh_status(preview['publication_id'], 'alice')
    return service, meta, preview, asset, execute, poll


@pytest.mark.parametrize('accounts', [['pixelpapa-facebook'], ['pixelpapa-instagram'], ['pixelpapa-facebook', 'pixelpapa-instagram']])
async def test_import_once_preview_and_independent_final_links(publisher, accounts):
    service, meta, preview, asset, execute, poll = await setup_reels(publisher, accounts)
    assert preview['preview']['request']['account_ids'] == sorted(accounts)
    assert len(preview['preview']['assets']) == 1
    assert preview['preview']['assets'][0]['sha256'] == asset['sha256']
    assert asset['metadata']['fast_start'] and not asset['metadata']['has_edit_list']
    await execute()  # Sessions, persisted before bytes.
    assert not meta.uploaded
    await execute()  # Binary upload only.
    ready = await poll()  # GETs may mark readiness but never publish.
    assert all(t['checkpoint']['stage'].endswith('_reel_ready') for t in ready['targets'])
    assert all(t['next_action'] == 'execute_next_step' for t in ready['targets'])
    assert not meta.published
    await execute()  # Existing explicit workflow continues; final mutations.
    final = await poll()
    assert final['state'] == 'succeeded'
    for target in final['targets']:
        platform = target['account_id'].split('-')[-1]
        assert target['delivery_confirmed']
        assert target['result']['permalink'].startswith('https://www.')
        assert target['attempts'] == 3
        if platform == 'instagram':
            assert target['checkpoint']['container_id'] == '202'
        assert meta.calls[platform + ':start'] == meta.calls[platform + ':upload'] == meta.calls[platform + ':publish'] == 1
    counts = meta.calls.copy()
    await execute()
    assert meta.calls == counts  # Successful target is never dispatched again.
    with service.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Post)) == len(accounts)
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.operation == 'reconcile_publication_stage')) == len(accounts)


async def test_independent_ambiguous_instagram_publish_never_duplicates_facebook(publisher):
    service, meta, preview, _, execute, poll = await setup_reels(publisher)
    meta.fail_publish = 'instagram'
    await execute(); await execute(); await poll(); await execute()
    final = await poll()
    states = {t['account_id']: t for t in final['targets']}
    assert states['pixelpapa-facebook']['state'] == 'succeeded'
    assert states['pixelpapa-instagram']['state'] == 'uncertain'
    assert states['pixelpapa-instagram']['error']['code'] == 'write_outcome_unknown'
    assert states['pixelpapa-instagram']['next_action'] == 'manual_reconciliation'
    await execute(); await poll(); await execute()
    assert meta.calls['facebook:publish'] == meta.calls['instagram:publish'] == 1
    assert meta.calls['instagram:start'] == meta.calls['instagram:upload'] == 1
    assert 'pretend secret' not in str(service.review(preview['publication_id'], 'alice'))


@pytest.mark.parametrize('platform', ['facebook', 'instagram'])
async def test_ambiguous_upload_recovers_with_gets_without_resending(publisher, platform):
    _, meta, _, _, execute, poll = await setup_reels(publisher, ['pixelpapa-' + platform])
    meta.fail_upload = platform
    meta.processing = True
    meta.upload_receiving = True
    await execute()
    uncertain = await execute()
    assert uncertain['targets'][0]['state'] == 'uncertain'
    await execute()
    assert meta.calls[platform + ':upload'] == 1 and not meta.published
    meta.processing = False
    meta.upload_receiving = False
    ready = await poll()
    assert ready['targets'][0]['checkpoint']['stage'].endswith('_reel_ready')
    await execute()
    assert (await poll())['state'] == 'succeeded'
    assert meta.calls[platform + ':upload'] == meta.calls[platform + ':publish'] == 1


async def test_ambiguous_facebook_finish_reconciles_without_second_finish(publisher):
    _, meta, _, _, execute, poll = await setup_reels(publisher, ['pixelpapa-facebook'])
    meta.fail_publish = 'facebook'
    await execute(); await execute(); await poll()
    assert (await execute())['state'] == 'succeeded'  # Immediate GET proves delivery.
    await execute(); await poll()
    assert meta.calls['facebook:publish'] == 1


async def test_delayed_facebook_finish_timeout_is_never_sent_again(publisher):
    _, meta, _, _, execute, poll = await setup_reels(publisher, ['pixelpapa-facebook'])
    meta.fail_publish = 'facebook'
    meta.pending_publication = True
    await execute(); await execute(); await poll()
    pending = await execute()
    assert pending['targets'][0]['checkpoint']['stage'] == 'fb_reel_publishing'
    await execute(); await poll(); await execute()
    assert meta.calls['facebook:publish'] == 1
    meta.pending_publication = False
    assert (await poll())['state'] == 'succeeded'


async def test_one_platform_permission_failure_does_not_repeat_successful_target(publisher):
    _, meta, _, _, execute, poll = await setup_reels(publisher)
    meta.reject_publish = 'instagram'
    await execute(); await execute(); await poll(); await execute()
    assert (await poll())['state'] == 'partial_failure'
    await execute(); await poll()
    assert meta.calls['facebook:publish'] == meta.calls['instagram:publish'] == 1


async def test_processing_reads_work_with_all_publication_switches_disabled(publisher):
    service, meta, _, _, execute, poll = await setup_reels(publisher)
    await execute(); await execute()
    service.settings.meta_live_writes = service.settings.publishing_enabled = False
    service.settings.instagram_reels_publishing_enabled = service.settings.facebook_media_publishing_enabled = False
    ready = await poll()
    assert all(t['checkpoint']['stage'].endswith('_reel_ready') for t in ready['targets'])
    assert not meta.published


async def test_readiness_assessment_uses_get_only_and_distinguishes_resumable_eligibility(publisher):
    from scripts.verify_reels_readiness import assess
    service = publisher[0]
    def handler(r):
        assert r.method == 'GET'
        if r.url.path.endswith('/' + PAGE_ID):
            return httpx.Response(200, json={'id': PAGE_ID, 'instagram_business_account': {'id': INSTAGRAM_ID}})
        if r.url.path.endswith('/content_publishing_limit'):
            return httpx.Response(200, json={'data': [{'quota_usage': 0}]})
        return httpx.Response(200, json={'id': INSTAGRAM_ID, 'username': 'pixelpapamusic'})
    adapters = {p: adapter(p, handler)[0] for p in ('facebook', 'instagram')}
    for a in adapters.values(): a.graph.allow_writes = False
    service.connector_factory = lambda account, execute: adapters[account.platform]
    result = await assess(service)
    assert result['status'] == 'passed' and result['writes_performed'] == 0
    assert result['resumable_eligibility'] == 'not_verified_until_separately_authorized_session_creation'


async def test_status_never_uploads_or_finishes_and_media_can_expire_after_upload(publisher):
    service, meta, _, asset, execute, poll = await setup_reels(publisher)
    await execute()
    await poll()
    assert not meta.uploaded
    await execute()
    # All bytes are acknowledged, so later processing doesn't depend on private storage.
    service.media.path(asset['id'])[0].unlink()
    await poll(); await poll()
    assert not meta.published
    await execute()
    assert (await poll())['state'] == 'succeeded'


async def test_expired_approval_blocks_finish_but_allows_status_reads(publisher):
    _, meta, _, _, execute, poll = await setup_reels(publisher)
    await execute(); await execute()
    publisher[3][0] += timedelta(hours=2)
    await poll()
    with pytest.raises(HubError) as error: await execute()
    assert error.value.code == 'authorization_required'
    assert not meta.published


async def test_ig_account_allowlist_does_not_enable_image_publishing(publisher):
    service = publisher[0]
    assert not service.media_write_allowed({'media_ids': ['synthetic'], 'account_ids': ['pixelpapa-instagram'], 'content_type': 'image'})
    service.settings.instagram_reels_publishing_enabled = False
    assert not service.media_write_allowed({'media_ids': ['synthetic'], 'account_ids': ['pixelpapa-instagram'], 'content_type': 'reel'})


@pytest.mark.parametrize('platform', ['facebook', 'instagram'])
@pytest.mark.parametrize('kind', ['wrong-host', 'wrong-version', 'wrong-id', 'query', 'credentials', 'redirect'])
async def test_upload_session_pins_host_version_id_and_has_no_credentials(platform, kind):
    edge = 'video-upload' if platform == 'facebook' else 'ig-api-upload'
    url = f'https://rupload.facebook.com/{edge}/v26.0/123'
    url = {'wrong-host': url.replace('rupload.facebook.com', 'evil.example'),
           'wrong-version': url.replace('v26.0', 'v25.0'), 'wrong-id': url.replace('/123', '/456'),
           'query': url + '?secret=never-log', 'credentials': url.replace('https://', 'https://user:secret@'),
           'redirect': 'http://rupload.facebook.com/' + edge + '/v26.0/123'}[kind]
    def handler(r):
        assert r.url.host == 'graph.facebook.com'
        return httpx.Response(200, json={'video_id': '123', 'id': '123', 'upload_url': url, 'uri': url})
    a, aid = adapter(platform, handler)
    with pytest.raises(HubError) as error: await a.step(aid, request('reel'), {})
    assert error.value.code == 'write_outcome_unknown' and 'secret' not in error.value.message


async def test_changed_binary_refused_before_network(tmp_path):
    path = tmp_path / 'video'; path.write_bytes(b'changed')
    a, _ = adapter('facebook', lambda r: pytest.fail('No network before hash validation'))
    with pytest.raises(HubError) as error:
        await a.upload_reel_file('123', {'_media_path': str(path), '_media_size': 7, '_media_sha256': sha256(b'original').hexdigest()})
    assert error.value.code == 'invalid_media' and error.value.diagnostics['mutation_attempted'] is False


@pytest.mark.parametrize('platform,duration,valid', [('facebook', 3, False), ('facebook', 4, True), ('facebook', 61, False), ('instagram', 3, True), ('instagram', 600, True), ('instagram', 601, False)])
async def test_platform_specific_duration_limits(publisher, platform, duration, valid):
    service = publisher[0]
    asset = service.media.save(Path('tests/fixtures/reel-h264-aac-5s.mp4'), 'alice')
    asset['metadata']['duration'] = duration  # Deliberate metadata contract boundary test.
    a, aid = adapter(platform, lambda r: pytest.fail('Validation must not write'))
    if valid: await a.validate(aid, request('reel'), [asset])
    else:
        with pytest.raises(HubError) as error: await a.validate(aid, request('reel'), [asset])
        assert error.value.code == 'invalid_media'


@pytest.mark.parametrize('field,value', [('frame_rate', 22), ('frame_rate', 61), ('fast_start', False), ('has_edit_list', True), ('pixel_format', 'yuv444p'), ('video_bit_rate', 26000000), ('video_codec', 'hevc')])
async def test_unsupported_media_rejected_before_session_creation(publisher, field, value):
    asset = publisher[0].media.save(Path('tests/fixtures/reel-h264-aac-5s.mp4'), 'alice')
    asset['metadata'][field] = value
    a, aid = adapter('instagram', lambda r: pytest.fail('No network'))
    with pytest.raises(HubError): await a.validate(aid, request('reel'), [asset])


def test_mp4_layout_bounds_and_edit_list_detection(tmp_path):
    def box(kind, body=b''): return struct.pack('>I4s', 8 + len(body), kind) + body
    path = tmp_path / 'mp4'
    path.write_bytes(box(b'ftyp') + box(b'moov', box(b'trak', box(b'edts', box(b'elst')))) + box(b'mdat'))
    assert mp4_layout(path) == {'fast_start': True, 'has_edit_list': True}
    path.write_bytes(box(b'ftyp') + box(b'mdat') + box(b'moov'))
    assert mp4_layout(path)['fast_start'] is False
    path.write_bytes(struct.pack('>I4s', 999, b'moov'))
    with pytest.raises(ValueError): mp4_layout(path)


@pytest.mark.parametrize('platform', ['facebook', 'instagram'])
async def test_missing_permalink_needs_only_reads_not_republication(platform):
    available = [False]
    def handler(r):
        assert r.method == 'GET'
        return httpx.Response(200, json={'permalink_url' if platform == 'facebook' else 'permalink':
            ('https://www.facebook.com/reel/123/' if platform == 'facebook' else 'https://www.instagram.com/reel/example/') if available[0] else None})
    a, aid = adapter(platform, handler)
    checkpoint = {'stage': 'delivered', 'remote_id': '123'}
    assert (await a.refresh_delivery(aid, request('reel'), checkpoint))['state'] == 'processing'
    available[0] = True
    assert (await a.refresh_delivery(aid, request('reel'), checkpoint))['state'] == 'succeeded'


@pytest.mark.parametrize('status', [302, 429, 500])
async def test_binary_upload_failure_is_not_retried_or_redirected(tmp_path, status):
    path = Path('tests/fixtures/reel-h264-aac-5s.mp4')
    calls = []
    def handler(r):
        calls.append(r)
        return httpx.Response(status, headers={'Location': 'https://evil.example/'}, json={'success': False})
    a, _ = adapter('facebook', handler)
    with pytest.raises(HubError) as error:
        await a.upload_reel_file('123', {'_media_path': str(path), '_media_size': path.stat().st_size, '_media_sha256': sha256(path.read_bytes()).hexdigest()})
    assert error.value.code == 'write_outcome_unknown' and len(calls) == 1


async def test_read_adapter_cannot_upload_even_with_local_file():
    a, _ = adapter('facebook', lambda r: pytest.fail('Network forbidden'))
    a.graph.allow_writes = False
    with pytest.raises(HubError) as error: await a.upload_reel_file('123', {})
    assert error.value.code == 'live_writes_disabled'


@pytest.mark.parametrize('field,value', [('has_edit_list', True), ('fast_start', False), ('audio_bit_rate', 129000)])
async def test_facebook_does_not_apply_instagram_container_or_audio_limits(publisher, field, value):
    asset = publisher[0].media.save(Path('tests/fixtures/reel-h264-aac-5s.mp4'), 'alice')
    if field == 'audio_bit_rate':
        asset['metadata']['audio_details'][0]['bit_rate'] = value
    else:
        asset['metadata'][field] = value
    fb, page = adapter('facebook', lambda r: pytest.fail('Validation must not contact Meta'))
    ig, account = adapter('instagram', lambda r: pytest.fail('Validation must not contact Meta'))
    await fb.validate(page, request('reel'), [asset])
    with pytest.raises(HubError) as error: await ig.validate(account, request('reel'), [asset])
    assert error.value.code == 'invalid_media'


async def test_uploaded_facebook_reel_is_ready_for_finish_before_processing_starts(publisher):
    service, meta, preview, _, execute, poll = await setup_reels(publisher, ['pixelpapa-facebook'])
    await execute(); await execute()
    # Exact ordering observed from Meta: upload complete, processing not_started.
    before = meta.calls.copy()
    ready = await poll()
    target = ready['targets'][0]
    assert target['checkpoint']['stage'] == 'fb_reel_ready'
    assert target['next_action'] == 'execute_next_step' and target['attempts'] == 2
    assert meta.calls['facebook:start'] == before['facebook:start'] == 1
    assert meta.calls['facebook:upload'] == before['facebook:upload'] == 1
    assert meta.calls['facebook:publish'] == 0  # Status never finishes autonomously.
    meta.processing = True
    finishing = await execute()
    assert finishing['targets'][0]['checkpoint']['stage'] == 'fb_reel_publishing'
    assert finishing['targets'][0]['attempts'] == 3
    pending = await poll()
    assert pending['state'] == 'in_progress' and meta.calls['facebook:publish'] == 1
    await execute()  # Already acknowledged finish: GET-only refresh, no duplicate.
    meta.processing = False
    assert (await poll())['state'] == 'succeeded'
    assert meta.calls['facebook:start'] == meta.calls['facebook:upload'] == meta.calls['facebook:publish'] == 1


async def test_uncertain_facebook_finish_does_not_become_ready_again_when_processing_not_started():
    def handler(r):
        assert r.method == 'GET'
        return httpx.Response(200, json={'status': {'video_status': 'upload_complete',
            'uploading_phase': {'status': 'complete'}, 'processing_phase': {'status': 'not_started'},
            'publishing_phase': {'status': 'not_started'}}})
    a, aid = adapter('facebook', handler)
    state = await a.reel_refresh(aid, request('reel'), {'stage': 'fb_reel_ready', 'remote_id': '123'}, uncertain=True)
    assert state['checkpoint']['stage'] == 'fb_reel_publishing' and state['uncertain']


@pytest.mark.parametrize('checkpoint', ['fb_reel_upload', 'fb_reel_processing', 'fb_reel_ready',
    'fb_reel_publishing', 'delivered', 'succeeded'])
async def test_process_restart_resumes_every_facebook_reel_checkpoint_without_duplicates(publisher, checkpoint):
    from app.services.publishing import PublishingService
    service, meta, preview, _, execute, poll = await setup_reels(publisher, ['pixelpapa-facebook'])
    await execute()
    if checkpoint != 'fb_reel_upload': await execute()
    if checkpoint not in {'fb_reel_upload', 'fb_reel_processing'}: await poll()
    if checkpoint in {'fb_reel_publishing', 'delivered', 'succeeded'}: await execute()
    if checkpoint == 'delivered': meta.permalink_ready = False
    if checkpoint in {'delivered', 'succeeded'}: await poll()
    before = service.review(preview['publication_id'], 'alice')
    assert (before['state'] == 'succeeded') if checkpoint == 'succeeded' else before['targets'][0]['checkpoint']['stage'] == checkpoint
    # New coordinator and connectors; only the database and remote provider survive.
    restarted = PublishingService(service.settings, service.sessions,
        lambda account, execute: adapter(account.platform, meta)[0], clock=service.clock)
    meta.permalink_ready = True
    pid = preview['publication_id']
    for _ in range(8):
        publisher[3][0] += timedelta(seconds=61)
        result = await restarted.refresh_status(pid, 'alice')
        if result['state'] == 'succeeded': break
        assert result['targets'][0]['state'] != 'uncertain'
        if result['targets'][0]['next_action'] == 'execute_next_step':
            await restarted.execute('alice', ['socialhub:publish'], pid)
    assert result['state'] == 'succeeded' and result['request_hash'] == preview['request_hash']
    assert result['targets'][0]['attempts'] == 3
    counts = meta.calls.copy()
    for _ in range(3): await restarted.execute('alice', ['socialhub:publish'], pid)
    assert meta.calls == counts
    assert meta.calls['facebook:start'] == meta.calls['facebook:upload'] == meta.calls['facebook:publish'] == 1
    with service.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Publication)) == 1
        assert db.scalar(select(func.count()).select_from(Post)) == 1


@pytest.mark.parametrize('stage', ['initial', 'fb_reel_upload', 'fb_reel_ready'])
@pytest.mark.parametrize('provider_accepted', [False, True])
async def test_expired_reel_mutation_lease_never_resends_unknown_outcome(publisher, stage, provider_accepted):
    service, meta, preview, _, execute, poll = await setup_reels(publisher, ['pixelpapa-facebook'])
    if stage != 'initial': await execute()
    if stage == 'fb_reel_ready': await execute(); await poll()
    tid = preview['targets'][0]['target_id']
    claim = service.claim(preview['publication_id'], tid, 'alice')
    assert claim  # Worker crashes after claim, before durably recording its outcome.
    if provider_accepted and stage == 'initial': meta.calls['facebook:start'] += 1
    if provider_accepted and stage == 'fb_reel_upload':
        meta.uploaded.add('facebook'); meta.calls['facebook:upload'] += 1
    if provider_accepted and stage == 'fb_reel_ready':
        meta.published.add('facebook'); meta.calls['facebook:publish'] += 1
    counts = {k: meta.calls[k] for k in ['facebook:start', 'facebook:upload', 'facebook:publish']}
    publisher[3][0] += timedelta(minutes=6)
    result = await execute()
    if provider_accepted and stage == 'fb_reel_upload':
        assert result['targets'][0]['checkpoint']['stage'] == 'fb_reel_ready'
    elif provider_accepted and stage == 'fb_reel_ready':
        assert result['state'] == 'succeeded'
    else:
        assert result['targets'][0]['state'] == 'uncertain'
    for _ in range(2): await service.refresh_status(preview['publication_id'], 'alice')
    assert {k: meta.calls[k] for k in counts} == counts  # No POST despite known remote ID.


@pytest.mark.parametrize('stage', ['initial', 'fb_reel_upload', 'fb_reel_ready'])
async def test_duplicate_execute_during_live_reel_lease_does_not_dispatch(publisher, stage):
    service, meta, preview, _, execute, poll = await setup_reels(publisher, ['pixelpapa-facebook'])
    if stage != 'initial': await execute()
    if stage == 'fb_reel_ready': await execute(); await poll()
    assert service.claim(preview['publication_id'], preview['targets'][0]['target_id'], 'alice')
    counts = meta.calls.copy()
    await execute(); await execute()
    assert meta.calls == counts


async def test_deleted_delivered_reel_remains_cached_and_cannot_be_republished(publisher):
    service, meta, preview, _, execute, poll = await setup_reels(publisher, ['pixelpapa-facebook'])
    await execute(); await execute(); await poll(); await execute()
    assert (await poll())['state'] == 'succeeded'
    with service.sessions.begin() as db:
        target=db.get(PublicationTarget,preview['targets'][0]['target_id'])
        target.result=dict(target.result,cleanup={'state':'deleted'})
    def forbidden(r): pytest.fail('A delivered/deleted Reel must never contact Meta again')
    service.connector_factory=lambda account,execute:adapter(account.platform,forbidden)[0]
    for _ in range(3):
        cached=await service.refresh_status(preview['publication_id'],'alice')
        replay=await service.execute('alice',['socialhub:publish'],preview['publication_id'])
        for result in [cached,replay]:
            assert result['state']=='succeeded' and result['request_hash']==preview['request_hash']
            assert result['targets'][0]['attempts']==3 and result['targets'][0]['next_action']=='none'
            assert result['targets'][0]['result']['cleanup']['state']=='deleted'
