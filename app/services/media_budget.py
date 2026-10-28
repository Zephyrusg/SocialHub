"""Cross-process upload admission, disk quotas and bounded temporary retention."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import os
from pathlib import Path
import shutil
import tempfile
import time
from uuid import UUID
from sqlalchemy import select
from app.core import HubError
from app.database.models import Media, PublicationAsset, PublicationTarget, utcnow


class MediaBudget:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions
        self.root = Path(settings.media_root)

    @contextmanager
    def locked(self):
        fd = None
        try:
            if not self.root.is_absolute() or self.root.is_symlink():
                raise OSError()
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.stat().st_mode & 0o777 != 0o700:
                self.root.chmod(0o700)
            fd = os.open(self.root / '.media.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if fd is not None: os.close(fd)
            raise HubError('media_busy', 'Another media transfer or cleanup is active; retry later', True) from None
        except OSError:
            if fd is not None: os.close(fd)
            raise HubError('media_storage_unavailable', 'Private media storage is unavailable') from None
        try:
            yield
        finally:
            os.close(fd)

    @contextmanager
    def upload(self):
        with self.locked():
            marker = os.open(self.root / '.retention-enabled', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(marker)
            self.cleanup_locked()
            size, count = 0, 0
            with os.scandir(self.root) as entries:
                for entry in entries:
                    if entry.name in {'.media.lock', '.retention-enabled'}: continue
                    count += 1
                    size += entry.stat(follow_symlinks=False).st_size
                    if count > self.settings.media_storage_max_files: break
            reserve = self.settings.media_max_bytes
            if count >= self.settings.media_storage_max_files or size + reserve > self.settings.media_storage_max_bytes:
                raise HubError('media_quota_exceeded', 'Temporary media storage is full; wait for expiry or use a smaller configured file limit', True)
            if shutil.disk_usage(self.root).free < reserve + self.settings.media_min_free_bytes:
                raise HubError('media_storage_full', 'Insufficient free space for a bounded media transfer', True)
            fd, name = tempfile.mkstemp(prefix='.upload-', dir=self.root)
            os.close(fd)
            try:
                yield Path(name)
            finally:
                Path(name).unlink(missing_ok=True)

    def cleanup(self):
        with self.locked():
            return self.cleanup_locked()

    def cleanup_locked(self):
        removed, expired = 0, 0
        now = utcnow()
        # Only generated staging names or UUID files are ever removed.
        with self.sessions.begin() as db:
            with os.scandir(self.root) as entries:
                for index, entry in enumerate(entries):
                    if index >= 256: break
                    if not entry.is_file(follow_symlinks=False): continue
                    path = self.root / entry.name
                    age = time.time() - entry.stat(follow_symlinks=False).st_mtime
                    if entry.name.startswith(('.upload-', '.probe-')):
                        if age > self.settings.media_stale_upload_seconds:
                            path.unlink(missing_ok=True); removed += 1
                        continue
                    try: UUID(entry.name)
                    except ValueError: continue
                    item = db.get(Media, entry.name)
                    if item is None:
                        if age > self.settings.media_stale_upload_seconds:
                            path.unlink(missing_ok=True); removed += 1
                    elif item.metadata_json.get('_bytes_deleted'):
                        if age > self.settings.media_stale_upload_seconds:
                            path.unlink(missing_ok=True); removed += 1
                    elif item.expires_at is None and not item.metadata_json.get('_bytes_deleted'):
                        item.expires_at = datetime.fromtimestamp(entry.stat(follow_symlinks=False).st_mtime, timezone.utc).replace(tzinfo=None) + timedelta(seconds=self.settings.media_retention_seconds)
            for item in db.scalars(select(Media).where(Media.expires_at <= now).limit(256)):
                active = db.scalar(select(PublicationTarget.id).join(PublicationAsset,
                    PublicationAsset.publication_id == PublicationTarget.publication_id).where(
                    PublicationAsset.media_id == item.id, PublicationTarget.state == 'sending',
                    PublicationTarget.lease_until > now).limit(1))
                if active: continue
                path = self.root / item.id
                if path.parent != self.root or path.name != item.id: continue
                try: UUID(item.id)
                except ValueError: continue
                path.unlink(missing_ok=True)
                item.metadata_json = dict(item.metadata_json, _bytes_deleted=True)
                item.expires_at = None
                expired += 1
        return {'expired_media': expired, 'stale_files': removed}
