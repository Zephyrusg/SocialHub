"""Persistent, restrictive runtime preferences; deployment policy is the ceiling."""
import json
import re
from cryptography.fernet import InvalidToken
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from app.core import HubError
from app.database.models import SiteConfiguration, Audit
from app.security.tokens import TokenVault

# Labels describe existing operations, not new publishing or scheduling support.
FEATURES = {
    'facebook_publishing': ('Facebook Publishing', 'Pause all Facebook publication, edit and reply execution.', True),
    'facebook_images': ('Facebook Images', 'Publish a validated image with a caption; requires signed media delivery.', True),
    'facebook_videos': ('Facebook Videos', 'Publish a regular Facebook video; requires signed media delivery.', True),
    'facebook_reels': ('Facebook Reels', 'Publish a validated MP4 using the native Reel workflow.', True),
    'instagram_publishing': ('Instagram Publishing', 'Pause Instagram publication execution; individual format switches still apply.', True),
    'instagram_photos': ('Instagram Photos', 'Publish one validated JPEG feed photo after explicit approval; requires signed delivery.', True),
    'instagram_story_artwork': ('Instagram Music Story Artwork', 'Prepare vertical artwork and a phone download. Choose the official Music track and publish manually in Instagram.', False),
    'youtube_publishing': ('YouTube Publishing', 'Upload only to the verified allowlisted channel, under the reviewed visibility.', True),
    'youtube_shorts': ('YouTube Shorts', 'Upload square/vertical MP4 up to three minutes; YouTube determines classification.', True),
    'youtube_videos': ('YouTube Standard Videos', 'Upload validated videos with structured metadata and resumable progress.', True),
    'youtube_thumbnails': ('YouTube Custom Thumbnails', 'Custom thumbnail upload is not implemented in this release.', True),
    'youtube_scheduling': ('YouTube Scheduling', 'Native publishAt scheduling is not implemented in this release.', True),
    'media_import': ('Media Import', 'Import ChatGPT attachments or authenticated uploads into expiring private storage.', False),
    'media_normalization': ('Instagram Media Normalization', 'Create an optional Instagram-compatible MP4 copy; video is preserved and AAC audio may be re-encoded. Never publishes.', False),
    'chatgpt_direct': ('ChatGPT Direct Publishing', 'Use the deployed direct-instruction policy for its exact authorized owner and client. This is not independent proof of consent.', True),
    'media_delivery': ('Signed Media Delivery', 'Serve bounded, authenticated or signed downloads. Disabling can interrupt provider fetches already in progress.', False),
    'attachment_probe': ('Attachment Validation Probe', 'Download and validate an allowlisted ChatGPT attachment without storing it.', False),
    'scheduling': ('Scheduling', 'Live scheduling is not implemented; existing mock jobs do not publish.', True),
}
DYNAMIC_FIELDS = {'instagram_photos_enabled', 'instagram_story_artwork_enabled', 'publishing_enabled', 'facebook_media_publishing_enabled', 'facebook_media_allowed_types',
    'instagram_reels_publishing_enabled', 'media_ingestion_enabled', 'media_delivery_enabled',
    'media_attachment_probe_enabled', 'media_normalization_enabled', 'approval_mode',
    'youtube_publishing_enabled','youtube_shorts_enabled','youtube_videos_enabled'}


def deployment_settings(settings):
    base = getattr(settings, 'base', settings)
    return getattr(base, '_deployment_settings', None) or base


def load_flags(base, db):
    row = db.get(SiteConfiguration, 'features')
    try:
        values = json.loads(TokenVault([base.encryption_key.get_secret_value()]).cipher.decrypt(row.ciphertext.encode())) if row else {}
        if not isinstance(values, dict) or set(values) - (set(FEATURES) - UNSUPPORTED) or any(type(v) is not bool for v in values.values()):
            raise ValueError()
    except (InvalidToken, ValueError, TypeError, AttributeError):
        raise HubError('configuration_invalid', 'Stored feature configuration could not be verified') from None
    return (row.revision if row else 0), values


UNSUPPORTED = {'scheduling', 'youtube_thumbnails', 'youtube_scheduling'}
OFF_DEFAULT = {'instagram_photos', 'instagram_story_artwork', 'media_normalization','youtube_publishing','youtube_shorts','youtube_videos','youtube_thumbnails','youtube_scheduling'}

def states(base, values):
    live = base.meta_live_writes and base.publishing_enabled
    fb = live and 'pixelpapa-facebook' in base.meta_write_account_ids
    ig = live and 'pixelpapa-instagram' in base.meta_write_account_ids and (base.instagram_reels_publishing_enabled or base.instagram_photos_enabled)
    delivery = base.media_delivery_enabled
    direct = bool(base.approval_mode == 'chatgpt_direct' and base.auth_mode == 'oauth'
        and base.chatgpt_direct_tested and base.chatgpt_residual_risk_accepted
        and re.fullmatch(r'[a-f0-9]{64}', base.chatgpt_direct_evidence_sha256)
        and base.chatgpt_allowed_principals and base.chatgpt_allowed_client_ids)
    available = {'facebook_publishing': fb, 'instagram_publishing': ig,
        'media_normalization': bool(base.media_normalization_enabled and base.media_ingestion_enabled
                                    and len(base.media_normalization_secret.get_secret_value()) >= 64),
        'media_import': base.media_ingestion_enabled, 'chatgpt_direct': direct,
        'media_delivery': delivery, 'attachment_probe': bool(base.media_attachment_probe_enabled and base.media_download_hosts),
        'scheduling': False}
    available['instagram_photos'] = bool(ig and base.instagram_photos_enabled and delivery and base.media_public_base_url)
    available['instagram_story_artwork'] = bool(base.instagram_story_artwork_enabled and base.media_ingestion_enabled and delivery and base.media_public_base_url)
    for kind in ['image', 'video', 'reel']:
        available['facebook_' + {'image':'images', 'video':'videos', 'reel':'reels'}[kind]] = bool(
            fb and base.facebook_media_publishing_enabled and kind in base.facebook_media_allowed_types
            and (kind == 'reel' or (delivery and base.media_public_base_url)))
    yt = bool(base.youtube_enabled and base.youtube_publishing_enabled and base.youtube_channel_ids and 'pixelpapa-youtube' in base.youtube_write_account_ids and base.publishing_enabled)
    available.update(youtube_publishing=yt, youtube_shorts=yt and base.youtube_shorts_enabled,
        youtube_videos=yt and base.youtube_videos_enabled, youtube_thumbnails=False, youtube_scheduling=False)
    result = {name: {'name':name, 'label':label, 'description':description, 'publishing':publishing,
        'implemented':name not in UNSUPPORTED, 'configured':values.get(name), 'default_enabled':name not in OFF_DEFAULT, 'available':bool(available[name]),
        'effective':bool(available[name] and values.get(name, name not in OFF_DEFAULT)),
        'reason': '' if available[name] else ('Not implemented for live publishing.' if name in UNSUPPORTED else 'Blocked by deployment settings, account restrictions or required dependencies.')}
        for name, (label, description, publishing) in FEATURES.items()}
    for name in ['facebook_images', 'facebook_videos', 'facebook_reels']:
        result[name]['effective'] &= result['facebook_publishing']['effective']
        if name != 'facebook_reels': result[name]['effective'] &= result['media_delivery']['effective']
    result['instagram_photos']['effective'] &= result['instagram_publishing']['effective'] and result['media_delivery']['effective']
    result['instagram_story_artwork']['effective'] &= result['media_import']['effective'] and result['media_delivery']['effective']
    for name in ['youtube_shorts','youtube_videos']:
        result[name]['effective'] &= result['youtube_publishing']['effective']
    # New conversion is opt-in even before any persistent preference exists.
    result['media_normalization']['effective'] = bool(available['media_normalization']
        and values.get('media_normalization', False) and result['media_import']['effective'])
    for name, row in result.items():
        if row['available'] and not row['effective'] and values.get(name, name not in OFF_DEFAULT):
            row['reason'] = 'A required runtime feature is disabled.'
    if available['media_normalization'] and not values.get('media_normalization', False):
        result['media_normalization']['reason'] = 'Off until explicitly enabled by an administrator.'
    return result


def apply_flags(result, base, db):
    revision, values = load_flags(base, db)
    flags = states(base, values)
    enabled = lambda name: flags[name]['effective']
    # No stored preference must preserve existing deployment behavior exactly.
    # Availability can be narrower than a raw switch (e.g. missing hosted URL).
    changes = {'media_ingestion_enabled': base.media_ingestion_enabled and values.get('media_import', True),
        'media_normalization_enabled': enabled('media_normalization'),
        'media_delivery_enabled': base.media_delivery_enabled and values.get('media_delivery', True),
        'media_attachment_probe_enabled': base.media_attachment_probe_enabled and values.get('attachment_probe', True),
        'facebook_media_allowed_types': [kind for kind, name in [('image','images'),('video','videos'),('reel','reels')]
            if kind in base.facebook_media_allowed_types and values.get('facebook_'+name, True)],
        'facebook_media_publishing_enabled': base.facebook_media_publishing_enabled and values.get('facebook_publishing', True),
        'instagram_reels_publishing_enabled': base.instagram_reels_publishing_enabled and values.get('instagram_publishing', True)}
    changes['instagram_photos_enabled'] = enabled('instagram_photos')
    changes['instagram_story_artwork_enabled'] = enabled('instagram_story_artwork')
    for key in ['youtube_publishing_enabled','youtube_shorts_enabled','youtube_videos_enabled']:
        changes[key] = enabled(key.removesuffix('_enabled'))
    if any(n in values for n in ['facebook_publishing','instagram_publishing']):
        changes['publishing_enabled'] = base.publishing_enabled and (enabled('facebook_publishing') or enabled('instagram_publishing') or enabled('youtube_publishing'))
    if base.approval_mode == 'chatgpt_direct' and not values.get('chatgpt_direct', True):
        changes['approval_mode'] = 'owner_required'
    for key, value in changes.items():setattr(result, key, value)
    result._feature_configuration_revision = revision
    result._feature_states = flags
    result._deployment_settings = base
    return result


class FeatureFlags:
    def __init__(self, settings, sessions):
        self.base, self.sessions = deployment_settings(settings), sessions

    def authorize(self, subject):
        if not self.base.admin_enabled or subject not in self.base.admin_allowed_subjects:
            raise HubError('admin_denied', 'An authorized SocialHub administrator is required')

    def status(self, subject):
        self.authorize(subject)
        with self.sessions() as db:revision, values = load_flags(self.base, db)
        return {'revision':revision, 'features':list(states(self.base, values).values())}

    def save(self, subject, revision, name, enabled, *, acknowledged=False):
        self.authorize(subject)
        if type(revision) is not int or revision < 0 or name not in FEATURES or name in UNSUPPORTED or type(enabled) is not bool:
            raise HubError('invalid_feature', 'Unsupported feature or malformed preference')
        if enabled and FEATURES[name][2] and acknowledged is not True:
            raise HubError('feature_confirmation_required', 'Review the publishing warning before enabling this feature')
        try:
            with self.sessions.begin() as db:
                # Serialize MariaDB edits; CAS also protects SQLite and competing stale forms.
                row = db.scalar(select(SiteConfiguration).where(SiteConfiguration.id == 'features').with_for_update())
                current, values = load_flags(self.base, db)
                if current != revision:
                    raise HubError('configuration_conflict', 'Features changed; reload and review before saving')
                before = states(self.base, values)[name]
                if enabled and not before['available']:
                    raise HubError('feature_unavailable', 'Deployment policy does not permit this feature')
                if row is None:
                    db.add(SiteConfiguration(id='features', revision=0, ciphertext=TokenVault([self.base.encryption_key.get_secret_value()]).encrypt({})))
                    db.flush()
                values[name] = enabled
                result = db.execute(update(SiteConfiguration).where(SiteConfiguration.id == 'features', SiteConfiguration.revision == revision)
                    .values(revision=revision+1, ciphertext=TokenVault([self.base.encryption_key.get_secret_value()]).encrypt(values)))
                if result.rowcount != 1:
                    raise HubError('configuration_conflict', 'Concurrent feature update refused')
                after = states(self.base, values)[name]
                db.add(Audit(subject=subject, operation='admin_feature_updated', outcome='success', details={
                    'feature':name, 'previous_value':before['configured'], 'new_value':enabled,
                    'previous_effective':before['effective'], 'new_effective':after['effective'], 'revision':revision+1}))
        except IntegrityError:
            raise HubError('configuration_conflict', 'Concurrent feature initialization refused; reload the page') from None
        except OperationalError as error:
            code = error.orig.args[0] if error.orig.args else None
            if code in {1205, 1213} or getattr(error.orig, 'sqlite_errorcode', None) in {5, 6}:
                raise HubError('configuration_conflict', 'Concurrent feature transaction refused; reload the page') from None
            raise
        return revision+1
