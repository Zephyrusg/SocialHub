"""Validated private media and purpose-separated, short-lived download signatures."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import time
import warnings
from uuid import uuid4
from urllib.parse import urlencode
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.core import HubError
from app.database.models import Media

Image.MAX_IMAGE_PIXELS = 40_000_000


def probe_command(path):
    return ['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-max_alloc', '67108864',
            '-probesize', '5000000', '-analyzeduration', '5000000', '-show_entries',
            'stream=codec_type,codec_name,width,height:format=duration,format_name', '-of', 'json', str(path)]


class MediaManager:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions
        self.root = Path(settings.media_root)

    def inspect(self, path):
        size = path.stat().st_size
        if size < 1 or size > self.settings.media_max_bytes:
            raise HubError('invalid_media', 'Media is empty or exceeds the configured limit')
        with path.open('rb') as f:
            signature = f.read(16)
        if signature.startswith((b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n')):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(path) as image:
                        width, height, fmt = image.width, image.height, image.format
                        image.verify()
                    with Image.open(path) as image:
                        image.load()  # Verify decoded bytes, not just a signature.
                if fmt not in {'JPEG', 'PNG'} or min(width, height) < 1:
                    raise ValueError()
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                raise HubError('invalid_media', 'Image cannot be safely decoded as JPEG or PNG') from None
            return ('image/jpeg' if fmt == 'JPEG' else 'image/png'), {'validated': True, 'width': width, 'height': height, 'format': fmt}
        if signature[4:8] != b'ftyp':
            raise HubError('invalid_media', 'Only decoded JPEG/PNG and validated MP4 are supported')
        try:
            process = subprocess.run(probe_command(path),
                capture_output=True, timeout=20, check=True)
            data = json.loads(process.stdout)
            video = next(s for s in data['streams'] if s.get('codec_type') == 'video')
            audio = [s['codec_name'] for s in data['streams'] if s.get('codec_type') == 'audio']
            duration = float(data['format']['duration'])
            if video['codec_name'] != 'h264' or any(a != 'aac' for a in audio) or not 0 < duration <= 600 or min(video['width'], video['height']) < 1:
                raise ValueError()
            return 'video/mp4', {'validated': True, 'width': video['width'], 'height': video['height'], 'duration': duration, 'video_codec': 'h264', 'audio_codecs': audio, 'format': 'MP4'}
        except FileNotFoundError:
            raise HubError('media_validator_unavailable', 'ffprobe is required for video validation; use the Docker runtime') from None
        except (subprocess.SubprocessError, ValueError, KeyError, StopIteration):
            raise HubError('invalid_media', 'MP4 must contain H.264 video and optional AAC audio, with duration at most ten minutes') from None

    def save(self, temporary_path, principal='local-owner'):
        temporary_path = Path(temporary_path)
        mime, metadata = self.inspect(temporary_path)
        metadata['owners'] = [principal]
        digest = self.file_hash(temporary_path)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        mid = str(uuid4())
        destination = self.root / mid
        try:
            with self.sessions.begin() as db:
                existing = db.scalar(select(Media).where(Media.sha256 == digest))
                if existing:
                    previous = dict(existing.metadata_json)
                    previous.update({k: v for k, v in metadata.items() if k != 'owners'})
                    previous['owners'] = sorted(set(previous.get('owners', [])) | {principal})
                    existing.metadata_json = previous
                    return self.describe(existing)
                media = Media(id=mid, sha256=digest, path=str(destination), mime_type=mime,
                              size=temporary_path.stat().st_size, metadata_json=metadata)
                db.add(media)
                db.flush()
                # Source upload staging is on the same private volume; no caller filename is used.
                with temporary_path.open('rb') as source, destination.open('xb') as out:
                    os.chmod(destination, 0o600)
                    while block := source.read(1024 * 1024):
                        out.write(block)
                result = self.describe(media)
            return result
        except IntegrityError:
            destination.unlink(missing_ok=True)
            with self.sessions.begin() as db:
                existing = db.scalar(select(Media).where(Media.sha256 == digest).with_for_update())
                if existing:
                    metadata = dict(existing.metadata_json)
                    metadata['owners'] = sorted(set(metadata.get('owners', [])) | {principal})
                    existing.metadata_json = metadata
                    return self.describe(existing)
            raise HubError('media_conflict', 'Concurrent media registration conflicted; retry safely') from None
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    @staticmethod
    def file_hash(path):
        with Path(path).open('rb') as f:
            return hashlib.file_digest(f, 'sha256').hexdigest()

    @staticmethod
    def describe(media):
        return {'id': media.id, 'sha256': media.sha256, 'mime_type': media.mime_type, 'size': media.size, 'metadata': {k: v for k, v in media.metadata_json.items() if k != 'owners'}}

    def get(self, mid, principal=None):
        with self.sessions() as db:
            item = db.get(Media, mid)
            if not item or (principal is not None and principal not in item.metadata_json.get('owners', [])):
                raise HubError('media_not_found', 'Private media asset not found')
            return self.describe(item)

    def path(self, mid, expected_hash=None):
        with self.sessions() as db:
            item = db.get(Media, mid)
            if not item or not item.metadata_json.get('validated'):
                raise HubError('media_not_found', 'Validated private media asset not found')
            path = Path(item.path)
            if path.is_symlink() or path.resolve().parent != self.root.resolve() or not path.is_file():
                raise HubError('invalid_media', 'Asset path is outside private storage or missing')
            digest = self.file_hash(path)
            if digest != item.sha256 or (expected_hash and digest != expected_hash):
                raise HubError('media_changed', 'Media bytes changed after preview; prepare a new publication')
            return path, item.mime_type

    def signature(self, mid, checksum, expires):
        key = hmac.new(self.settings.encryption_key.get_secret_value().encode(), b'socialhub-media-v1', hashlib.sha256).digest()
        return hmac.new(key, f'{mid}:{checksum}:{expires}'.encode(), hashlib.sha256).hexdigest()

    def temporary_url(self, mid, checksum, ttl=900):
        if not self.settings.media_public_base_url:
            raise HubError('media_delivery_unconfigured', 'Configure an explicitly reachable HTTPS media base before executing media publication')
        expires = int(time.time()) + min(max(ttl, 30), 900)
        query = urlencode({'expires': expires, 'signature': self.signature(mid, checksum, expires)})
        return self.settings.media_public_base_url.rstrip('/') + f'/media/{mid}?' + query

    def verify_signature(self, mid, expires, signature):
        if not self.settings.media_public_base_url:
            raise HubError('media_delivery_disabled', 'Signed media delivery is disabled')
        try:
            expiration = int(expires)
        except (ValueError, TypeError):
            raise HubError('media_access_denied', 'Invalid media expiry') from None
        if not int(time.time()) < expiration <= int(time.time()) + 900:
            raise HubError('media_access_denied', 'Media download authorization expired or exceeds the allowed lifetime')
        asset = self.get(mid)
        expected = self.signature(mid, asset['sha256'], expiration)
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise HubError('media_access_denied', 'Invalid media download signature')
        return self.path(mid, asset['sha256'])
