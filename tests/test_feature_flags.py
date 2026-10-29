"""Runtime preferences cannot widen deployment policy or substitute for consent."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re

import httpx
import pytest
from sqlalchemy import select, event
from app.core import HubError
from app.database.models import SiteConfiguration, Audit, PublicationTarget
from app.main import create_server
from app.services.feature_flags import FeatureFlags
from app.services.administration import AdminSessions, EffectiveSettings, effective_settings
from app.services.media_ingestion import MediaIngestion
from app.services.hub import Hub
from app.security.tokens import TokenVault
from test_administration import config
from test_production_auth import ORIGIN
from test_publishing import publisher, prepare, approve, image_asset
from test_attachment_ingestion import media_settings, downloader, reference


def settings(sessions, **changes):
    return config(sessions, admin_allowed_subjects=['owner-sub'], meta_live_writes=True,
        publishing_enabled=True, meta_write_account_ids=['pixelpapa-facebook'],
        facebook_media_publishing_enabled=True, facebook_media_allowed_types=['image','video','reel'],
        media_ingestion_enabled=True, media_public_base_url=ORIGIN, **changes)


def by_name(status):
    return {f['name']:f for f in status['features']}


def test_unset_preferences_preserve_existing_deployment_and_roles(sessions):
    s = settings(sessions)
    before = s.model_dump()
    flags = FeatureFlags(s, sessions)
    status = flags.status('owner-sub'); rows = by_name(status)
    assert status['revision'] == 0 and all(r['configured'] is None for r in rows.values())
    assert rows['facebook_publishing']['effective'] and rows['facebook_reels']['effective']
    assert not rows['instagram_publishing']['available'] and not rows['scheduling']['implemented']
    assert effective_settings(s, sessions).model_dump() == before
    with sessions() as db: assert db.get(SiteConfiguration, 'features') is None
    for actor in ['wife-sub', 'other-admin', 'outsider']:
        with pytest.raises(HubError): flags.status(actor)
        with pytest.raises(HubError): flags.save(actor, 0, 'facebook_publishing', False)
        with pytest.raises(HubError): AdminSessions(s, sessions).create_session(actor)


def test_preferences_persist_audit_atomically_and_conflict(sessions):
    s = settings(sessions); f = FeatureFlags(s, sessions)
    assert f.save('owner-sub', 0, 'facebook_publishing', False) == 1
    recreated = FeatureFlags(s, sessions)
    assert not by_name(recreated.status('owner-sub'))['facebook_publishing']['effective']
    active = effective_settings(s, sessions)
    assert not active.publishing_enabled and s.publishing_enabled
    assert active.meta_write_account_ids == s.meta_write_account_ids
    assert active.admin_allowed_subjects == ['owner-sub']
    with pytest.raises(HubError) as error:f.save('owner-sub', 0, 'facebook_publishing', True, acknowledged=True)
    assert error.value.code == 'configuration_conflict'
    with sessions() as db:
        audit = db.scalar(select(Audit).where(Audit.operation=='admin_feature_updated'))
        assert audit.subject == 'owner-sub' and audit.at
        assert audit.details == {'feature':'facebook_publishing', 'previous_value':None, 'new_value':False,
            'previous_effective':True, 'new_effective':False, 'revision':1}
        encrypted = db.get(SiteConfiguration, 'features').ciphertext
        assert 'facebook_publishing' not in encrypted
    assert f.save('owner-sub', 1, 'facebook_publishing', True, acknowledged=True) == 2
    assert effective_settings(s, sessions).publishing_enabled


@pytest.mark.parametrize('name', ['instagram_publishing','chatgpt_direct','scheduling', 'account_allowlist', 'approval_mode'])
def test_unavailable_and_fundamental_controls_cannot_be_enabled(sessions, name):
    f = FeatureFlags(settings(sessions), sessions)
    with pytest.raises(HubError):f.save('owner-sub', 0, name, True, acknowledged=True)
    assert f.status('owner-sub')['revision'] == 0


@pytest.mark.parametrize('value', ['false', 0, 1, None])
def test_strict_inputs_and_enable_warning(sessions, value):
    f = FeatureFlags(settings(sessions), sessions)
    with pytest.raises(HubError):f.save('owner-sub', 0, 'facebook_publishing', value, acknowledged=True)
    with pytest.raises(HubError):f.save('owner-sub', 0, 'facebook_publishing', True)
    assert f.status('owner-sub')['revision'] == 0


def test_deployment_prohibition_wins_over_stored_true(sessions):
    s = settings(sessions); f = FeatureFlags(s, sessions)
    f.save('owner-sub', 0, 'facebook_images', True, acknowledged=True)
    s.facebook_media_publishing_enabled = False
    row = by_name(f.status('owner-sub'))['facebook_images']
    assert row['configured'] is True and not row['available'] and not row['effective']
    assert not effective_settings(s, sessions).facebook_media_publishing_enabled
    with pytest.raises(HubError):f.save('owner-sub', 1, 'facebook_images', True, acknowledged=True)


def test_competing_initial_edits_have_one_winner(sessions):
    f = FeatureFlags(settings(sessions), sessions)
    def edit(name):
        try:return f.save('owner-sub', 0, name, False)
        except HubError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ['facebook_publishing', 'media_import']))
    assert sorted(map(str, results)) == ['1', 'configuration_conflict']
    with sessions() as db:
        assert len(list(db.scalars(select(Audit).where(Audit.operation=='admin_feature_updated')))) == 1


def test_audit_failure_rolls_back_preference(sessions):
    f = FeatureFlags(settings(sessions), sessions)
    def fail(*_):raise RuntimeError('synthetic audit failure')
    event.listen(Audit, 'before_insert', fail)
    try:
        with pytest.raises(RuntimeError):f.save('owner-sub', 0, 'media_import', False)
    finally:event.remove(Audit, 'before_insert', fail)
    assert f.status('owner-sub')['revision'] == 0
    with sessions() as db:assert db.get(SiteConfiguration, 'features') is None


@pytest.mark.parametrize('ciphertext', ['corrupted', None])
def test_corrupt_configuration_fails_closed(sessions, ciphertext):
    s = settings(sessions)
    with sessions.begin() as db:db.add(SiteConfiguration(id='features', revision=1, ciphertext=ciphertext))
    with pytest.raises(HubError):effective_settings(s, sessions)
    with pytest.raises(HubError):FeatureFlags(s, sessions).save('owner-sub', 1, 'media_import', True)


async def test_ui_owner_csrf_origin_warning_and_stale_confirmation(sessions):
    s = settings(sessions); auth = AdminSessions(s, sessions); token = auth.create_session('owner-sub')
    app = create_server(s).streamable_http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        assert (await c.post('/admin/features', data={})).status_code == 403
        c.cookies.set('__Secure-socialhub-admin', token)
        page = await c.get('/admin/features')
        assert page.status_code == 200 and '<h1>Feature Flags</h1>' in page.text
        assert 'Inherit deployment' in page.text and 'Unavailable' in page.text
        assert 'role="switch"' in page.text and 'Scheduling' in page.text
        csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page.text)[1]
        data = dict(csrf=csrf, revision='0', feature='facebook_publishing', enabled='false', confirmation='no')
        for headers, changed in [({}, {}), ({'Origin':'https://evil.test'}, {}), ({'Origin':ORIGIN}, {'csrf':'forged'}), ({'Origin':ORIGIN}, {'extra':'true'})]:
            assert (await c.post('/admin/features', data=data|changed, headers=headers)).status_code == 403
        assert (await c.post('/admin/features', data=data, headers={'Origin':ORIGIN})).status_code == 303
        enable = data | dict(revision='1', enabled='true')
        warning = await c.post('/admin/features', data=enable, headers={'Origin':ORIGIN})
        assert warning.status_code == 200 and 'Confirm enable' in warning.text
        assert not effective_settings(s, sessions).publishing_enabled
        FeatureFlags(s, sessions).save('owner-sub', 1, 'media_import', False)
        assert (await c.post('/admin/features', data=enable|{'confirmation':'yes'}, headers={'Origin':ORIGIN})).status_code == 409
        assert (await c.post('/admin/features', data=enable|{'revision':'2','confirmation':'yes'}, headers={'Origin':ORIGIN})).status_code == 303
        assert effective_settings(s, sessions).publishing_enabled
        unavailable = enable | dict(revision='3', feature='instagram_publishing', confirmation='yes')
        assert (await c.post('/admin/features', data=unavailable, headers={'Origin':ORIGIN})).status_code == 403
        assert (await c.post('/admin/features', content='csrf='+csrf+'&csrf='+csrf, headers={'Origin':ORIGIN,'Content-Type':'application/x-www-form-urlencoded'})).status_code == 403


async def test_disabling_facebook_stops_existing_service_without_mutation(publisher):
    service, _, calls, _ = publisher
    service.settings.admin_enabled=True; service.settings.admin_allowed_subjects=['owner-sub']
    preview = await prepare(service); approve(service, preview)
    flags=FeatureFlags(service.settings, service.sessions)
    flags.save('owner-sub', 0, 'facebook_publishing', False)
    assert not service.review(preview['publication_id'], 'alice')['live_writes_enabled']
    with pytest.raises(HubError):await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert not calls and service.review(preview['publication_id'], 'alice')['targets'][0]['attempts'] == 0
    flags.save('owner-sub', 1, 'facebook_publishing', True, acknowledged=True)
    await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert len(calls) == 1


@pytest.mark.parametrize('kind,flag', [('image','facebook_images'), ('video','facebook_videos'), ('reel','facebook_reels')])
async def test_content_gates_block_real_execution_path(publisher, tmp_path, kind, flag):
    service, _, calls, _=publisher
    s=service.settings;s.admin_enabled=True;s.admin_allowed_subjects=['owner-sub'];s.facebook_media_allowed_types=['image','video','reel']
    if kind=='image':asset=image_asset(service,tmp_path)
    else:asset=service.media.save(Path('tests/fixtures/reel-h264-aac-5s.mp4'),'alice')
    preview=await prepare(service, content_type=kind, media_ids=[asset['id']]);approve(service,preview)
    FeatureFlags(s,service.sessions).save('owner-sub',0,flag,False)
    with pytest.raises(HubError):await service.execute('alice',['socialhub:publish'],preview['publication_id'])
    assert not calls and service.review(preview['publication_id'],'alice')['targets'][0]['attempts']==0
    assert service.review(preview['publication_id'],'alice')['request_hash']==preview['request_hash']


async def test_import_and_private_delivery_pause_without_restart(media_settings, sessions):
    s=media_settings;s.admin_enabled=True;s.admin_allowed_subjects=['owner-sub']
    seen=[];ingestion=MediaIngestion(s,sessions,downloader(s,seen))
    flags=FeatureFlags(s,sessions);flags.save('owner-sub',0,'media_import',False)
    with pytest.raises(HubError) as error:await ingestion.import_file(reference(),'alice','paused')
    assert error.value.code=='media_ingestion_disabled' and not seen
    flags.save('owner-sub',1,'media_import',True)
    imported=await ingestion.import_file(reference(),'alice','resumed')
    assert imported['media']['id'] and len(seen)==1
    flags.save('owner-sub',2,'media_delivery',False)
    assert not EffectiveSettings(s,sessions).media_delivery_enabled


async def test_disable_during_validation_does_not_dispatch_or_lose_checkpoint(publisher):
    service, adapters, calls, now=publisher
    s=service.settings;s.admin_enabled=True;s.admin_allowed_subjects=['owner-sub']
    preview=await prepare(service);approve(service,preview)
    async def pause(*_):FeatureFlags(s,service.sessions).save('owner-sub',0,'facebook_publishing',False)
    adapters['facebook'].validate=pause
    result=await service.execute('alice',['socialhub:publish'],preview['publication_id'])
    assert not calls and result['targets'][0]['state']=='retryable'
    assert result['request_hash']==preview['request_hash']


async def test_direct_toggle_rechecks_existing_policy_grant(publisher):
    from test_approval_policy import native_settings, context
    service, _, calls, _ = publisher
    service.settings=native_settings(service, approval_mode='chatgpt_direct', chatgpt_direct_tested=True,
        chatgpt_direct_evidence_sha256='d'*64, chatgpt_confirmation_tested=False)
    service.settings.admin_enabled=True;service.settings.admin_allowed_subjects=['alice']
    preview=await prepare(service)
    await service.execute('alice',['socialhub:publish'],preview['publication_id'],context())
    assert len(calls)==1
    flags=FeatureFlags(service.settings,service.sessions)
    flags.save('alice',0,'chatgpt_direct',False)
    requirements=service.approval_requirements(preview['publication_id'],'alice',context())
    assert requirements['independent_owner_required'] and not requirements['current_grant_valid']
    with pytest.raises(HubError):await service.execute('alice',['socialhub:publish'],preview['publication_id'],context())
    assert len(calls)==1 and service.settings.approval_mode=='chatgpt_direct'
    flags.save('alice',1,'chatgpt_direct',True,acknowledged=True)
    assert (await service.execute('alice',['socialhub:publish'],preview['publication_id'],context()))['state']=='succeeded'
    assert len(calls)==2


async def test_direct_disable_during_validation_stops_dispatch(publisher):
    from test_approval_policy import native_settings, context
    service, adapters, calls, _=publisher
    s=native_settings(service,approval_mode='chatgpt_direct',chatgpt_direct_tested=True,
        chatgpt_direct_evidence_sha256='d'*64,chatgpt_confirmation_tested=False)
    s.admin_enabled=True;s.admin_allowed_subjects=['alice'];service.settings=s
    preview=await prepare(service)
    async def pause(*_):FeatureFlags(s,service.sessions).save('alice',0,'chatgpt_direct',False)
    adapters['facebook'].validate=pause
    result=await service.execute('alice',['socialhub:publish'],preview['publication_id'],context())
    assert not calls and result['targets'][0]['state']=='retryable'


async def test_instagram_pause_is_independent_and_reflected_in_capabilities(publisher, tmp_path):
    service, _, calls, _=publisher
    s=service.settings;s.admin_enabled=True;s.admin_allowed_subjects=['owner-sub']
    asset=image_asset(service,tmp_path)
    preview=await prepare(service,accounts=['pixelpapa-instagram'],content_type='image',media_ids=[asset['id']])
    approve(service,preview)
    FeatureFlags(s,service.sessions).save('owner-sub',0,'instagram_publishing',False)
    with pytest.raises(HubError):await service.execute('alice',['socialhub:publish'],preview['publication_id'])
    active=effective_settings(s,service.sessions)
    assert active._feature_states['facebook_publishing']['effective'] and not active._feature_states['instagram_publishing']['effective']
    assert active.meta_write_account_ids==s.meta_write_account_ids and not calls
    hub=Hub(service.sessions,EffectiveSettings(s,service.sessions))
    caps=await hub.read('get_account_capabilities','pixelpapa-instagram')
    assert not caps['capabilities']['live_writes_enabled']


async def test_get_only_reel_reconciliation_survives_disabled_publishing(publisher):
    from test_reels import setup_reels
    service, meta, preview, asset, execute, poll=await setup_reels(publisher,['pixelpapa-facebook'])
    await execute();await execute()
    service.settings.admin_enabled=True;service.settings.admin_allowed_subjects=['owner-sub']
    before=meta.calls.copy()
    FeatureFlags(service.settings,service.sessions).save('owner-sub',0,'facebook_publishing',False)
    status=await poll()
    assert status['targets'][0]['checkpoint']['stage']=='fb_reel_ready'
    assert not status['live_writes_enabled']
    assert meta.calls['facebook:publish']==before['facebook:publish']
    assert meta.calls['facebook:upload']==before['facebook:upload']
    with pytest.raises(HubError):await execute()


async def test_http_upload_and_download_gates_apply_to_running_app(sessions, tmp_path):
    s=settings(sessions,media_root=str(tmp_path/'media'))
    flags=FeatureFlags(s,sessions)
    app=create_server(s).streamable_http_app()
    headers={'Authorization':'Bearer '+'a'*64}
    image=Path('tests/fixtures/portrait.jpg').read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as c:
        flags.save('owner-sub',0,'media_import',False)
        response=await c.post('/media/upload',content=image,headers=headers)
        assert response.status_code==404
        flags.save('owner-sub',1,'media_import',True)
        response=await c.post('/media/upload',content=image,headers=headers)
        assert response.status_code==200,response.text
        mid=response.json()['media']['id']
        assert (await c.get('/media/'+mid,headers=headers)).status_code==200
        flags.save('owner-sub',2,'media_delivery',False)
        assert (await c.get('/media/'+mid,headers=headers)).status_code==404
        flags.save('owner-sub',3,'media_delivery',True)
        assert (await c.get('/media/'+mid,headers=headers)).status_code==200


async def test_probe_paused_without_downloading(media_settings,sessions):
    s=media_settings;s.admin_enabled=True;s.admin_allowed_subjects=['owner-sub']
    seen=[];probe=MediaIngestion(s,sessions,downloader(s,seen))
    flags=FeatureFlags(s,sessions);flags.save('owner-sub',0,'attachment_probe',False)
    assert (await probe.probe(reference()))['mode']=='reference_only' and not seen
    flags.save('owner-sub',1,'attachment_probe',True)
    assert (await probe.probe(reference()))['download_verified'] and len(seen)==1
