"""Subtitle rendering and request binding must never reuse an incompatible asset."""
import hashlib
import json
from pathlib import Path
import pytest
from app.core import HubError
from app.database.models import Idempotency
from app.services.instagram_artwork import InstagramArtwork
from app.services.story_templates import render_cinematic, subtitle_layout
from test_attachment_ingestion import media_settings
from test_instagram_photos_stories import enable
from test_story_templates import cover

TITLE="It's Dangerous to Go Alone"


def test_hidden_is_default_and_announcement_is_not_rendered():
    source=cover()
    default=render_cinematic(source)
    assert default.tobytes()==render_cinematic(source, 'OUT NOW • PIXELPAPA').tobytes()
    assert default.tobytes()==render_cinematic(source, subtitle_mode='hidden', song_title=TITLE).tobytes()
    shown=render_cinematic(source, subtitle_mode='title', song_title=TITLE)
    assert default.tobytes()!=shown.tobytes()
    assert shown.tobytes()==render_cinematic(source,'ignored old announcement',subtitle_mode='title',song_title=TITLE).tobytes()


@pytest.mark.parametrize('title', ['', '   ', None])
def test_title_mode_requires_actual_title(title):
    with pytest.raises(HubError) as error:render_cinematic(cover(),subtitle_mode='title',song_title=title)
    assert error.value.code=='invalid_request'


def test_long_title_wraps_without_losing_words():
    title='A New Adventure Beyond the Mountains and Across the Sea Where the Stars Will Always Guide Us Home Again'
    lines,face,top=subtitle_layout(title)
    assert len(lines)>1 and ' '.join(lines)==title
    assert 32<=face.size<=40 and top<=480
    assert all(face.getlength(line)<=880 for line in lines)
    assert top+840<=1320  # Artist branding starts at y=1340.


@pytest.fixture
def artwork(media_settings,sessions):
    s=media_settings;s.instagram_story_artwork_enabled=True;s.media_public_base_url='https://media.example.test'
    enable(s,sessions,'instagram_story_artwork')
    service=InstagramArtwork(s,sessions)
    source=service.media.save(Path('tests/fixtures/portrait.png'),'alice')
    return service,source


async def test_subtitle_options_bind_idempotency_and_source_is_unchanged(artwork):
    service,source=artwork
    hidden=await service.prepare('alice',source['id'],'hidden')
    assert hidden['effective_artwork_inputs']=={'subtitle_mode':'hidden','song_title':'','announcement':''}
    assert hidden['announcement_rendered'] is False
    for kwargs in [{'subtitle_mode':'title','song_title':TITLE},{'song_title':TITLE}]:
        with pytest.raises(HubError) as error:await service.prepare('alice',source['id'],'hidden',**kwargs)
        assert error.value.code=='idempotency_conflict'
    shown=await service.prepare('alice',source['id'],'title',subtitle_mode='title',song_title=TITLE)
    assert shown['effective_artwork_inputs']['song_title']==TITLE and shown['subtitle_mode']=='title'
    assert shown['media']['sha256']!=hidden['media']['sha256'] and shown['published'] is False
    assert (await service.prepare('alice',source['id'],'title',subtitle_mode='title',song_title=TITLE))['reused']
    with pytest.raises(HubError):await service.prepare('alice',source['id'],'title',subtitle_mode='title',song_title='Another title')
    assert service.media.get(source['id'],'alice')==source


async def test_old_announcement_argument_is_accepted_but_never_drawn(artwork):
    service,source=artwork
    before=await service.prepare('alice',source['id'],'clean')
    after=await service.prepare('alice',source['id'],'legacy-argument',announcement='OUT NOW • PIXELPAPA')
    assert after['media']['sha256']==before['media']['sha256']
    assert after['announcement']=='OUT NOW • PIXELPAPA' and after['announcement_rendered'] is False
    with pytest.raises(HubError):await service.prepare('alice',source['id'],'clean',announcement='changed')


async def test_pre_subtitle_cinematic_key_cannot_return_its_older_announcement(artwork):
    service,source=artwork
    key='ig-art:'+hashlib.sha256(b'alice:pre-subtitle').hexdigest()
    digest=hashlib.sha256(json.dumps([source['id'],source['sha256'],'story','OUT NOW • PIXELPAPA','cinematic-release-v2']).encode()).hexdigest()
    with service.sessions.begin() as db:
        db.add(Idempotency(key=key,subject='alice',request_hash=digest,response={'media_id':source['id'],'render_profile':'cinematic-release-v2'}))
    with pytest.raises(HubError) as error:
        await service.prepare('alice',source['id'],'pre-subtitle',announcement='OUT NOW • PIXELPAPA')
    assert error.value.code=='idempotency_conflict'


async def test_validation_before_render_and_story_options_do_not_change_feed(artwork):
    service,source=artwork
    with pytest.raises(HubError):await service.prepare('alice',source['id'],'empty',subtitle_mode='title')
    with pytest.raises(HubError):await service.prepare('alice',source['id'],'bad-mode',subtitle_mode='automatic')
    with pytest.raises(HubError):await service.prepare('alice',source['id'],'feed-title',purpose='feed',subtitle_mode='title',song_title=TITLE)
    # Old feed idempotency records still work unchanged, including their exact v1 hash.
    service.settings.base.instagram_photos_enabled=True
    service.settings.base.meta_live_writes=service.settings.base.publishing_enabled=True
    service.settings.base.meta_write_account_ids=['pixelpapa-instagram']
    flags=enable(service.settings.base,service.sessions,'instagram_photos')
    first=await service.prepare('alice',source['id'],'feed-old',purpose='feed')
    assert first['render_profile']=='v1'
    identity='ig-art:'+hashlib.sha256(b'alice:feed-old').hexdigest()
    with service.sessions() as db:
        row=db.get(Idempotency,identity)
        assert row.request_hash==hashlib.sha256(json.dumps([source['id'],source['sha256'],'feed','','v1']).encode()).hexdigest()
    assert (await service.prepare('alice',source['id'],'feed-old',purpose='feed'))['media']==first['media']
    flags.save('owner',flags.status('owner')['revision'],'instagram_story_artwork',False)
    with pytest.raises(HubError):await service.prepare('alice',source['id'],'hidden')
