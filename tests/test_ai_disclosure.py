"""Disclosure is immutable publication content, never approval or a runtime override."""
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from pydantic import ValidationError

from app.core import HubError
from app.models_publication import PublicationRequest
from test_publishing import publisher, approve, prepare
from test_publishing_workflows import adapter, request
from test_reels import ReelMeta


@pytest.mark.parametrize('explicit,default,expected', [
    (None, True, True), (None, False, False), (True, False, True), (False, True, False),
])
async def test_resolved_disclosure_is_frozen_and_hash_bound(publisher, explicit, default, expected):
    service, _, _, _ = publisher
    service.settings.facebook_media_allowed_types = ['reel']
    service.settings.reels_ai_generated_default = default
    asset = service.media.save(Path('tests/fixtures/reel-h264-aac-5s.mp4'), 'alice')
    fields = dict(account_ids=['pixelpapa-facebook', 'pixelpapa-instagram'],
                  content_type='reel', media_ids=[asset['id']], text='Exact caption', is_ai_generated=explicit)
    preview = await service.prepare('alice', 'ai-reel', fields)
    assert preview['preview']['request']['is_ai_generated'] is expected
    assert preview['preview']['request']['text'] == 'Exact caption'
    service.settings.reels_ai_generated_default = not default
    stored = service.review(preview['publication_id'], 'alice')
    assert stored['request_hash'] == preview['request_hash']
    assert stored['preview']['request']['is_ai_generated'] is expected
    with pytest.raises(HubError, match='Key is bound'):
        await service.prepare('alice', 'ai-reel', dict(fields, is_ai_generated=not expected))
    repeated = await service.prepare('alice', 'ai-reel', dict(fields, is_ai_generated=expected))
    assert repeated['publication_id'] == preview['publication_id']
    approve(service, preview)
    await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert service.review(preview['publication_id'], 'alice')['request_hash'] == preview['request_hash']


@pytest.mark.parametrize('platform', ['facebook', 'instagram'])
@pytest.mark.parametrize('value', [True, False, None])
async def test_disclosure_sent_at_correct_reel_step_and_legacy_omission(platform, value):
    meta, seen = ReelMeta(), []
    def handler(r):
        if r.method == 'POST':
            seen.append((r.url.path, parse_qs(r.content.decode())))
        return meta(r)
    connector, account = adapter(platform, handler)
    content = request('reel')
    if value is not None:
        content['is_ai_generated'] = value
    started = await connector.reel_step(account, content, {})
    meta.uploaded.add(platform)
    prefix = 'fb' if platform == 'facebook' else 'ig'
    ready = dict(started['checkpoint'], stage=prefix + '_reel_ready')
    await connector.reel_step(account, content, ready)
    for index, (_, form) in enumerate(seen):
        disclosure_step = index == (1 if platform == 'facebook' else 0)
        if disclosure_step and value is not None:
            assert form['is_ai_generated'] == [str(value).lower()]
        else:
            assert 'is_ai_generated' not in form


@pytest.mark.parametrize('value', ['false', 0, 1, 'true'])
def test_disclosure_requires_real_boolean(value):
    with pytest.raises(ValidationError):
        PublicationRequest(account_ids=['pixelpapa-facebook'], content_type='reel', media_ids=['asset'], is_ai_generated=value)


async def test_non_reel_payload_keeps_historical_shape(publisher):
    preview = await prepare(publisher[0])
    assert 'is_ai_generated' not in preview['preview']['request']
    with pytest.raises(ValidationError):
        PublicationRequest(account_ids=['pixelpapa-facebook'], text='text', is_ai_generated=True)
