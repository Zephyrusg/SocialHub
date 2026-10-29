"""Deterministic image derivatives. Music selection and Story publication stay in Instagram."""
import hashlib
import json
from PIL import Image, ImageOps
from app.core import HubError
from app.database.models import Idempotency, Audit
from app.services.administration import EffectiveSettings, require_publisher
from app.services.media_manager import MediaManager
from app.services.media_budget import MediaBudget
from app.services.media_ingestion import validation_thread
from app.services.story_templates import STORY_PROFILE, render_cinematic, validate_subtitle

ARTIST = 'PixelPapa'
TITLE = "It's Dangerous to Go Alone"
STEPS = [
    'Open this private transfer page on your phone before the link expires. Save the JPEG to Photos/Gallery, or use Share artwork if your browser supports sharing files.',
    'Open Instagram, confirm you are using @pixelpapamusic, create a Story and select the saved artwork. A share chooser may offer Instagram; if it does not, use Gallery.',
    'Tap Stickers → Music. Search for PixelPapa and It\'s Dangerous to Go Alone. Verify both the artist and recording, then listen to the preview. Library availability is unverified and may depend on account, region and release timing.',
    'Choose the official recording and select the excerpt inside Instagram. If it is missing, retry after release on October 30, 2026 and check with your distributor; do not select a similarly named recording or substitute embedded audio.',
    'Optionally add your release announcement, review the Story and finish publishing in Instagram. SocialHub cannot verify publication from a download or share action.',
]


class InstagramArtwork:
    def __init__(self, settings, sessions):
        self.settings = settings if isinstance(settings, EffectiveSettings) else EffectiveSettings(settings, sessions)
        self.sessions = sessions
        self.media = MediaManager(self.settings, sessions)
        self.budget = MediaBudget(self.settings, sessions)

    def guard(self, principal, purpose):
        require_publisher(self.settings, principal)
        active = self.settings.snapshot()
        field = 'instagram_photos_enabled' if purpose == 'feed' else 'instagram_story_artwork_enabled'
        if not active.media_ingestion_enabled or not getattr(active, field):
            raise HubError('instagram_artwork_disabled', 'Enable the applicable Instagram Photos or Music Story Artwork switch in Settings first')
        return active

    async def prepare(self, principal, media_id, key, purpose='story', announcement='', *, subtitle_mode='hidden', song_title=None):
        if purpose not in {'feed', 'story'} or not isinstance(key, str) or not 1 <= len(key) <= 128 or len(announcement) > 140 or (purpose == 'feed' and announcement):
            raise HubError('invalid_request', 'Choose feed or story, a bounded key, and an optional Story announcement of up to 140 characters')
        song_title = validate_subtitle(subtitle_mode, song_title)
        if purpose == 'feed' and (subtitle_mode != 'hidden' or song_title):
            raise HubError('invalid_request', 'Subtitle options are supported for Story artwork only')
        self.guard(principal, purpose)
        return await validation_thread(self.render, principal, media_id, key, purpose, announcement, subtitle_mode, song_title)

    def render(self, principal, media_id, key, purpose, announcement, subtitle_mode, song_title):
        asset = self.media.get(media_id, principal)
        source, mime = self.media.path(media_id, asset['sha256'])
        if mime not in {'image/jpeg', 'image/png'}:
            raise HubError('invalid_media', 'Choose an imported JPEG or PNG cover image')
        identity = 'ig-art:' + hashlib.sha256((principal + ':' + key).encode()).hexdigest()
        profile = STORY_PROFILE if purpose == 'story' else 'v1'
        inputs = [media_id, asset['sha256'], purpose, announcement, profile]
        # Feed hashes stay exactly v1. New Story hashes bind all subtitle inputs;
        # pre-subtitle keys cannot silently supply an older rendered subtitle.
        if purpose == 'story':
            inputs.extend([subtitle_mode, song_title])
        digest = hashlib.sha256(json.dumps(inputs).encode()).hexdigest()
        def cached():
            with self.sessions() as db:
                row = db.get(Idempotency, identity)
                if row is None:return None
                if row.subject != principal or row.request_hash != digest:
                    raise HubError('idempotency_conflict', 'Artwork key is already bound to different inputs or an older render profile; use a fresh key')
                return self.result(self.media.get(row.response['media_id'], principal), purpose, announcement, True, row.response.get('render_profile', 'v1'), subtitle_mode=subtitle_mode, song_title=song_title)
        previous = cached()
        if previous:return previous
        with self.budget.upload() as destination:
            previous = cached()
            if previous:return previous
            self.guard(principal, purpose)
            with Image.open(source) as original:
                image = ImageOps.exif_transpose(original).convert('RGBA')
                flattened = Image.new('RGBA', image.size, 'white')
                flattened.alpha_composite(image)
                image = flattened.convert('RGB')
            if purpose == 'feed':
                height = max(566, min(1350, round(1080 * image.height / image.width)))
                canvas = Image.new('RGB', (1080, height), 'white')
                image = ImageOps.contain(image, canvas.size, Image.Resampling.LANCZOS)
                canvas.paste(image, ((1080-image.width)//2, (height-image.height)//2))
            else:
                canvas = render_cinematic(image, announcement, subtitle_mode=subtitle_mode, song_title=song_title)
            canvas.save(destination, format='JPEG', quality=92, optimize=True)
            self.guard(principal, purpose)
            derived = self.media.save(destination, principal, consume=True)
            with self.sessions.begin() as db:
                db.add(Idempotency(key=identity, subject=principal, request_hash=digest, response={'media_id': derived['id'], 'render_profile': profile, **({'subtitle_mode': subtitle_mode, 'song_title': song_title} if purpose == 'story' else {})}))
                db.add(Audit(subject=principal, operation='prepare_instagram_artwork', outcome='success', details={
                    'source_media_id': media_id, 'media_id': derived['id'], 'sha256': derived['sha256'], 'purpose': purpose, 'render_profile': profile, 'published': False, **({'subtitle_mode': subtitle_mode, 'song_title': song_title} if purpose == 'story' else {})}))
            return self.result(derived, purpose, announcement, False, profile, subtitle_mode=subtitle_mode, song_title=song_title)

    def result(self, asset, purpose, announcement, reused, profile='v1', *, subtitle_mode='hidden', song_title=''):
        self.media.path(asset['id'], asset['sha256'])
        self.media.reserve_delivery(asset['id'])
        url = self.media.temporary_url(asset['id'], asset['sha256'])
        result = {'media': asset, 'purpose': purpose, 'reused': reused, 'published': False,
                  'status': 'artwork_prepared', 'render_profile': profile, 'preview_url': url, 'download_url': url + '&download=1',
                  'link_lifetime_seconds': 900}
        if purpose == 'story':
            result.update(transfer_url=url.replace('/media/', '/instagram/story/'),
                publication_mode='manual_in_instagram', native_music_api_supported=False,
                native_app_handoff_available=False, music_availability='unverified_manual_lookup_required',
                artist=ARTIST, song_title=TITLE, announcement=announcement, steps=STEPS,
                effective_artwork_inputs={'subtitle_mode': subtitle_mode, 'song_title': song_title, 'announcement': announcement},
                subtitle_mode=subtitle_mode, announcement_rendered=False)
        else:
            result.update(next_step='Use this media ID in a new image prepare_publication preview; review the caption and approve before execution.')
        return result
