"""Private filesystem ingestion interface; no upload HTTP endpoint in Phase 1."""
import hashlib
import os
from pathlib import Path
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.database.models import Media
from app.core import HubError


class MediaStore:
    def __init__(self, root, sessions, max_bytes=50 * 1024 * 1024):
        self.root, self.sessions, self.max_bytes = Path(root), sessions, max_bytes
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def ingest(self, content: bytes, mime_type: str):
        if not content or len(content) > self.max_bytes:
            raise HubError('invalid_media', 'Media is empty or exceeds size limit')
        valid = ((mime_type == 'image/jpeg' and content.startswith(b'\xff\xd8\xff')) or
                 (mime_type == 'image/png' and content.startswith(b'\x89PNG\r\n\x1a\n')) or
                 (mime_type == 'video/mp4' and content[4:8] == b'ftyp'))
        if not valid:
            raise HubError('invalid_media', 'Unsupported MIME type or file signature')
        digest = hashlib.sha256(content).hexdigest()
        mid = str(uuid4())
        path = self.root / mid
        try:
            with self.sessions.begin() as db:
                existing = db.scalar(select(Media).where(Media.sha256 == digest))
                if existing:
                    return existing.id
                db.add(Media(id=mid, sha256=digest, path=str(path), mime_type=mime_type, size=len(content)))
                db.flush()
                with path.open('xb') as output:
                    os.chmod(path, 0o600)
                    output.write(content)
            return mid
        except IntegrityError:
            path.unlink(missing_ok=True)
            with self.sessions() as db:
                existing = db.scalar(select(Media).where(Media.sha256 == digest))
                if existing:
                    return existing.id
            raise
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def temporary_url(self, media_id, expires_at):
        raise HubError('not_implemented', 'Private media serving requires a signed URL service and explicit exposure policy')
