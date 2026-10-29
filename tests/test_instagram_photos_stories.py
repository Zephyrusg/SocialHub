"""Creator photo publication and manual-only Music Story safety contracts."""
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
import httpx
import pytest
from PIL import Image
from sqlalchemy import select, func
from app.core import HubError
from app.database.models import Audit, Publication, PublicationTarget, Post
from app.services.feature_flags import FeatureFlags
from app.services.administration import effective_settings
from app.services.instagram_artwork import InstagramArtwork
from app.connectors.publishing import MetaPublishingConnector
from app.connectors.meta import GraphClient, InstagramConnector
from app.connectors.meta_policy import INSTAGRAM_ID
from test_publishing import publisher, image_asset, approve
from test_attachment_ingestion import media_settings


def enable(settings, sessions, *names):
    settings.admin_enabled = True
    settings.admin_allowed_subjects = ['owner']
    flags = FeatureFlags(settings, sessions)
    for name in names:
        flags.save('owner', flags.status('owner')['revision'], name, True, acknowledged=True)
    return flags


async def photo(publisher, tmp_path, fail=None):
    service, _, _, now = publisher
    s = service.settings
    s.instagram_photos_enabled = True
    flags = enable(s, service.sessions, 'instagram_photos')
    asset = image_asset(service, tmp_path)
    seen = []
    state = {'status': 'FINISHED'}
    def handler(r):
        seen.append(r)
        path = r.url.path.rsplit('/', 1)[-1]
        if r.method == 'POST' and path == 'media':
            assert b'image_url=' in r.content and b'video_url' not in r.content and b'media_type' not in r.content
            if fail == 'create':raise httpx.ReadTimeout('private')
            return httpx.Response(200, json={'id': '202'})
        if r.method == 'POST' and path == 'media_publish':
            if fail == 'publish':raise httpx.ReadTimeout('private')
            return httpx.Response(200, json={'id': '303'})
        if path == '202':return httpx.Response(200, json={'id': '202', 'status_code': state['status']})
        if path == '303':return httpx.Response(200, json={'id': '303', 'permalink': 'https://www.instagram.com/p/example/'})
        raise AssertionError(r.url)
    def factory(account, execute):
        return MetaPublishingConnector(InstagramConnector(GraphClient('synthetic', 'v26.0', transport=httpx.MockTransport(handler), allow_writes=execute), account.remote_id))
    service.connector_factory = factory
    preview = await service.prepare('alice', 'photo', {'account_ids':['pixelpapa-instagram'], 'content_type':'image', 'media_ids':[asset['id']], 'text':'Release October 30'})
    async def execute():return await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    async def poll():
        now[0] += timedelta(seconds=61)
        return await service.refresh_status(preview['publication_id'], 'alice')
    return service, flags, preview, seen, state, execute, poll


async def test_photo_creator_approval_checkpoint_and_idempotency(publisher, tmp_path):
    service, _, preview, seen, _, execute, poll = await photo(publisher, tmp_path)
    with pytest.raises(HubError):await execute()
    assert not seen
    approve(service, preview)
    first = await execute()
    assert first['targets'][0]['checkpoint']['stage'] == 'ig_photo_processing'
    ready = await poll()
    assert ready['targets'][0]['checkpoint']['stage'] == 'ig_photo_ready'
    assert sum(r.method == 'POST' for r in seen) == 1
    final = await execute()
    assert final['state'] == 'succeeded'
    await execute()
    assert sum(r.method == 'POST' for r in seen) == 2
    assert final['preview'] == preview['preview'] and final['request_hash'] == preview['request_hash']
    assert final['targets'][0]['checkpoint']['container_id'] == '202'


async def test_disable_photo_between_container_and_finish(publisher, tmp_path):
    service, flags, preview, seen, _, execute, poll = await photo(publisher, tmp_path)
    approve(service, preview)
    await execute(); await poll()
    flags.save('owner', flags.status('owner')['revision'], 'instagram_photos', False)
    with pytest.raises(HubError):await execute()
    assert sum(r.method == 'POST' for r in seen) == 1
    assert service.review(preview['publication_id'], 'alice')['preview'] == preview['preview']


@pytest.mark.parametrize('failure', ['create', 'publish'])
async def test_ambiguous_photo_is_never_resent(publisher, tmp_path, failure):
    service, _, preview, seen, _, execute, poll = await photo(publisher, tmp_path, failure)
    approve(service, preview)
    await execute()
    if failure == 'publish':await poll(); await execute()
    before = sum(r.method == 'POST' for r in seen)
    for _ in range(2):await execute(); await poll()
    result = service.review(preview['publication_id'], 'alice')
    assert result['state'] == 'attention_required'
    assert result['targets'][0]['next_action'] == 'manual_reconciliation'
    assert sum(r.method == 'POST' for r in seen) == before


@pytest.mark.parametrize('status', ['ERROR', 'EXPIRED'])
async def test_failed_photo_container_does_not_publish(publisher, tmp_path, status):
    service, _, preview, seen, state, execute, poll = await photo(publisher, tmp_path)
    approve(service, preview); await execute()
    state['status'] = status
    assert (await poll())['state'] == 'failed'
    await execute()
    assert sum(r.method == 'POST' for r in seen) == 1


async def test_photo_recovery_requires_published_container_unique_feed_image(publisher, tmp_path):
    service, _, preview, seen, _, execute, poll = await photo(publisher, tmp_path, 'publish')
    approve(service, preview); await execute(); await poll(); result = await execute()
    target = result['targets'][0]
    def reader(r):
        assert r.method == 'GET'
        if r.url.path.endswith('/202'):return httpx.Response(200, json={'id':'202','status_code':'PUBLISHED'})
        return httpx.Response(200, json={'data':[{'id':'303','caption':'Release October 30', 'media_type':'IMAGE', 'media_product_type':'FEED', 'permalink':'https://www.instagram.com/p/example/', 'timestamp':service.clock().replace(tzinfo=timezone.utc).isoformat()}]})
    adapter = MetaPublishingConnector(InstagramConnector(GraphClient('synthetic','v26.0',transport=httpx.MockTransport(reader)), INSTAGRAM_ID))
    service.connector_factory = lambda account, execute: adapter
    final = await service.reconcile(preview['publication_id'], target['target_id'], '303', 'owner', expected_hash=preview['request_hash'])
    assert final['state'] == 'succeeded'
    assert final['targets'][0]['checkpoint']['container_id'] == '202'
    assert sum(r.method == 'POST' for r in seen) == 2


async def test_artwork_flags_normalization_ownership_idempotency_and_manual_status(media_settings, sessions):
    s = media_settings
    s.instagram_story_artwork_enabled = True
    s.media_public_base_url = 'https://media.example.test'
    service = InstagramArtwork(s, sessions)
    original = service.media.save(Path('tests/fixtures/portrait.png'), 'alice')
    with pytest.raises(HubError):await service.prepare('alice', original['id'], 'story')
    flags = enable(s, sessions, 'instagram_story_artwork')
    result = await service.prepare('alice', original['id'], 'story', announcement='Out October 30, 2026')
    assert not result['published'] and result['status'] == 'artwork_prepared'
    assert result['music_availability'] == 'unverified_manual_lookup_required'
    assert result['artist'] == 'PixelPapa' and result['song_title'] == "It's Dangerous to Go Alone"
    assert not result['native_app_handoff_available'] and not result['native_music_api_supported']
    path, _ = service.media.path(result['media']['id'])
    with Image.open(path) as image:
        assert image.size == (1080, 1920) and image.format == 'JPEG'
        assert not image.getexif()
    again = await service.prepare('alice', original['id'], 'story', announcement='Out October 30, 2026')
    assert again['reused'] and again['media'] == result['media']
    with pytest.raises(HubError):await service.prepare('bob', original['id'], 'story')
    with pytest.raises(HubError):await service.prepare('alice', original['id'], 'story', announcement='changed')
    flags.save('owner', flags.status('owner')['revision'], 'instagram_story_artwork', False)
    with pytest.raises(HubError):await service.prepare('alice', original['id'], 'story')
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Publication)) == 0
        assert db.scalar(select(func.count()).select_from(Post)) == 0
        assert db.scalar(select(Audit).where(Audit.operation=='prepare_instagram_artwork')).details['published'] is False


async def test_feed_artwork_png_normalizes_and_preserves_source(publisher, tmp_path):
    service, _, _, _, _, _, _ = await photo(publisher, tmp_path)
    service.settings.media_ingestion_enabled = True
    art = InstagramArtwork(service.settings, service.sessions)
    source = image_asset(service, tmp_path, fmt='PNG')
    result = await art.prepare('alice', source['id'], 'feed', 'feed')
    asset = result['media']
    assert asset['mime_type'] == 'image/jpeg' and asset['metadata']['width'] == 1080
    assert 0.8 <= asset['metadata']['width']/asset['metadata']['height'] <= 1.91
    assert service.media.get(source['id'],'alice') == source


async def test_transfer_page_signature_expiry_flag_and_download(media_settings, sessions):
    s = media_settings
    s.instagram_story_artwork_enabled = True
    s.media_public_base_url = 'https://media.example.test'
    flags = enable(s, sessions, 'instagram_story_artwork')
    art = InstagramArtwork(s, sessions)
    source = art.media.save(Path('tests/fixtures/portrait.jpg'), 'local-developer')
    result = await art.prepare('local-developer', source['id'], 'transfer')
    from app.security.instagram_tools import register_instagram_tools
    from mcp.server.fastmcp import FastMCP
    from app.services.administration import EffectiveSettings
    from app.security.media_routes import register_media_routes
    server = FastMCP('test')
    register_instagram_tools(server, EffectiveSettings(s, sessions), sessions)
    register_media_routes(server, EffectiveSettings(s, sessions), sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.streamable_http_app()), base_url='https://media.example.test') as client:
        page = await client.get(result['transfer_url'])
        assert page.status_code == 200 and 'Finish your Music Story' in page.text
        assert 'navigator.canShare' in page.text and "It's Dangerous to Go Alone" in page.text
        assert "frame-ancestors 'none'" in page.headers['content-security-policy']
        download = await client.get(result['download_url'])
        assert download.status_code == 200 and 'attachment' in download.headers['content-disposition']
        assert (await client.get(urlsplit(result['transfer_url']).path)).status_code == 403
        assert (await client.get(result['transfer_url'].replace('signature=', 'signature=wrong'))).status_code == 403
        flags.save('owner', flags.status('owner')['revision'], 'instagram_story_artwork', False)
        assert (await client.get(result['transfer_url'])).status_code == 403


async def test_disable_photo_while_validation_awaits_stops_dispatch(publisher, tmp_path):
    service, flags, preview, seen, _, execute, _ = await photo(publisher, tmp_path)
    approve(service, preview)
    factory = service.connector_factory
    def with_pause(account, execute):
        adapter = factory(account, execute)
        validate = adapter.validate
        async def pause(*args):
            await validate(*args)
            flags.save('owner', flags.status('owner')['revision'], 'instagram_photos', False)
        if execute:adapter.validate = pause
        return adapter
    service.connector_factory = with_pause
    result = await execute()
    assert not seen and result['targets'][0]['state'] == 'retryable'


def test_rollout_seeds_explicit_off_and_ceiling_does_not_enable(media_settings, sessions):
    from scripts.initialize_instagram_features import initialize
    s = media_settings
    s.admin_enabled = True
    s.admin_allowed_subjects = ['owner']
    status = initialize(s, sessions, 'owner')
    assert status['revision'] == 2
    assert initialize(s, sessions, 'owner')['revision'] == 2
    s.instagram_photos_enabled = s.instagram_story_artwork_enabled = True
    s.meta_live_writes = s.publishing_enabled = True
    s.meta_write_account_ids = ['pixelpapa-instagram']
    s.media_public_base_url = 'https://media.example.test'
    flags = FeatureFlags(s, sessions)
    for row in flags.status('owner')['features']:
        if row['name'] in {'instagram_photos', 'instagram_story_artwork'}:
            assert row['available'] and row['configured'] is False and not row['effective']
    assert not effective_settings(s, sessions).instagram_photos_enabled
    with pytest.raises(HubError):initialize(s, sessions, 'outsider')


@pytest.mark.parametrize('dependency', ['media_delivery', 'instagram_publishing'])
async def test_photo_parent_flags_block_new_execution(publisher, tmp_path, dependency):
    service, flags, preview, seen, _, execute, _ = await photo(publisher, tmp_path)
    approve(service, preview)
    flags.save('owner', flags.status('owner')['revision'], dependency, False)
    with pytest.raises(HubError):await execute()
    assert not seen


@pytest.mark.parametrize('dependency', ['media_delivery', 'media_import'])
async def test_artwork_dependencies_block_preparation(media_settings, sessions, dependency):
    s = media_settings
    s.instagram_story_artwork_enabled = True
    s.media_public_base_url = 'https://media.example.test'
    flags = enable(s, sessions, 'instagram_story_artwork')
    art = InstagramArtwork(s, sessions)
    source = art.media.save(Path('tests/fixtures/portrait.jpg'), 'alice')
    flags.save('owner', flags.status('owner')['revision'], dependency, False)
    with pytest.raises(HubError):await art.prepare('alice', source['id'], 'blocked')


async def test_instagram_capability_discovery_tracks_photo_and_story_toggles(publisher, tmp_path):
    from app.services.hub import Hub
    from app.services.administration import EffectiveSettings
    service, flags, _, _, _, _, _ = await photo(publisher, tmp_path)
    s = service.settings
    s.media_ingestion_enabled = s.instagram_story_artwork_enabled = True
    hub = Hub(service.sessions, EffectiveSettings(s, service.sessions))
    async def caps():return (await hub.read('get_account_capabilities', 'pixelpapa-instagram'))['capabilities']
    enabled = await caps()
    assert enabled['publish_image_enabled'] and not enabled['prepare_story_artwork_enabled']
    assert enabled['story_publication_mode'] == 'manual_in_instagram'
    assert not enabled['publish_story_enabled'] and not enabled['native_story_music_api_supported']
    assert not enabled['music_library_lookup_supported']
    flags.save('owner', flags.status('owner')['revision'], 'instagram_photos', False)
    assert not (await caps())['publish_image_enabled']
    flags.save('owner', flags.status('owner')['revision'], 'instagram_story_artwork', True)
    assert (await caps())['prepare_story_artwork_enabled']
    flags.save('owner', flags.status('owner')['revision'], 'media_import', False)
    assert not (await caps())['prepare_story_artwork_enabled']
