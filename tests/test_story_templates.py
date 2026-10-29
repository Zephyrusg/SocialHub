"""Visual geometry, glyph coverage, immutable legacy assets and offline rendering."""
import hashlib
import json
from pathlib import Path
import pytest
from PIL import Image, ImageDraw, ImageOps, ImageStat
from app.core import HubError
from app.database.models import Idempotency
from app.services.story_templates import render_cinematic, cover_accent, subtitle_layout, font, _luminance, STORY_PROFILE
from app.services.instagram_artwork import InstagramArtwork
from test_attachment_ingestion import media_settings
from test_instagram_photos_stories import enable


def cover(size=(1080,1080), colors=('#084b59','#93edc9')):
    image = Image.new('RGB',size,colors[0]);draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,size[0]//3,size[1]//2), fill=colors[1])
    draw.ellipse((size[0]//3,size[1]//3,size[0]-1,size[1]-1), fill=colors[1])
    return image


@pytest.mark.parametrize('size', [(1080,1080),(600,1000),(1600,600)])
def test_sharp_foreground_keeps_entire_cover_and_original(size):
    source=cover(size);before=source.tobytes()
    rendered=render_cinematic(source)
    assert rendered.size==(1080,1920) and rendered.mode=='RGB'
    expected=ImageOps.contain(source,(840,840),Image.Resampling.LANCZOS)
    x,y=(1080-expected.width)//2,410+(840-expected.height)//2
    assert rendered.crop((x,y,x+expected.width,y+expected.height)).tobytes()==expected.tobytes()
    assert source.tobytes()==before


@pytest.mark.parametrize('colors', [('#084b59','#93edc9'),('#0d366b','#ffad42'),('#452361','#d99bfa')])
def test_background_is_colored_full_bleed_and_accent_is_readable(colors):
    source=cover(colors=colors);rendered=render_cinematic(source)
    # The bottom remains atmospheric artwork, not empty black bars.
    values=ImageStat.Stat(rendered.crop((0,1630,1080,1920)))
    assert max(values.mean)>30 and max(values.stddev)>1
    assert _luminance(cover_accent(source))>=.55
    assert len(rendered.crop((0,0,100,1920)).getcolors(192000))>100


def test_colors_follow_cover_instead_of_fixed_gold():
    warm=cover_accent(cover(colors=('#402337','#ff9d32')))
    cool=cover_accent(cover(colors=('#084b59','#93edc9')))
    assert warm!=cool and warm[0]>warm[2] and cool[1]>cool[0]


@pytest.mark.parametrize('title', ['', "It's Dangerous to Go Alone", 'New music, new adventures — available everywhere. Join us for a new chapter and celebrate the release together.'])
def test_all_text_ink_fits_safe_regions_and_never_overlaps_cover(monkeypatch,title):
    source=cover();bounds=[]
    original=ImageDraw.ImageDraw.text
    def record(self,xy,text,*args,**kwargs):
        if text.strip():bounds.append(self.textbbox(xy,text,font=kwargs['font']))
        return original(self,xy,text,*args,**kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',record)
    rendered=render_cinematic(source, subtitle_mode='title' if title else 'hidden', song_title=title)
    top=subtitle_layout(title)[2] if title else 410
    for left,upper,right,lower in bounds:
        assert 95<=left<right<=985
        assert 230<=upper<lower<1460
        assert lower<=top or upper>=top+840
    assert rendered.size==(1080,1920)


def test_known_punctuation_and_accents_have_real_glyphs():
    face=font('DejaVuSans.ttf',32)
    assert subtitle_layout('OUT NOW • PIXELPAPA — Café “été”…')
    missing=face.getmask('\uffff')
    for char in '•—é“”…':
        glyph=face.getmask(char)
        assert (glyph.size,bytes(glyph))!=(missing.size,bytes(missing))
    with pytest.raises(HubError,match='supported|plain text'):
        subtitle_layout('Unsupported \U00020000')
    with pytest.raises(HubError):subtitle_layout('hidden\u200bcontrol')


def test_overflow_is_refused_without_clipping():
    with pytest.raises(HubError,match='Shorten'):
        subtitle_layout('W'*140)


async def test_legacy_story_key_requires_fresh_key_and_preserves_old_asset(media_settings,sessions):
    s=media_settings;s.instagram_story_artwork_enabled=True;s.media_public_base_url='https://media.example.test'
    enable(s,sessions,'instagram_story_artwork');service=InstagramArtwork(s,sessions)
    source=service.media.save(Path('tests/fixtures/portrait.png'),'alice')
    legacy=service.media.save(Path('tests/fixtures/portrait.jpg'),'alice')
    identity='ig-art:'+hashlib.sha256(b'alice:old-story').hexdigest()
    digest=hashlib.sha256(json.dumps([source['id'],source['sha256'],'story','old announcement','v1']).encode()).hexdigest()
    with sessions.begin() as db:
        db.add(Idempotency(key=identity,subject='alice',request_hash=digest,response={'media_id':legacy['id']}))
    with pytest.raises(HubError) as conflict:
        await service.prepare('alice',source['id'],'old-story','story','old announcement')
    assert conflict.value.code=='idempotency_conflict'
    assert service.media.get(legacy['id'],'alice')==legacy
    current=await service.prepare('alice',source['id'],'fresh-story','story','old announcement')
    assert not current['reused'] and current['render_profile']==STORY_PROFILE
    assert current['media']['id']!=legacy['id'] and not current['published']
    assert (await service.prepare('alice',source['id'],'fresh-story','story','old announcement'))['media']==current['media']
    with pytest.raises(HubError):await service.prepare('alice',source['id'],'old-story','story','changed')


def test_bundled_fonts_have_license_and_load_without_system_font_lookup():
    from app.services.story_templates import FONTS
    assert 'Permission is hereby granted' in (FONTS/'LICENSE.txt').read_text()
    assert all(font(name,40).getlength('PIXELPAPA')>0 for name in ['DejaVuSans.ttf','DejaVuSans-Bold.ttf','DejaVuSerif-Bold.ttf'])
