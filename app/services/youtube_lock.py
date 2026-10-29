"""Cross-process nonblocking upload concurrency bound on the existing media volume."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import os
from app.core import HubError

@contextmanager
def upload_lock(root):
    p=Path(root);p.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(p/'.youtube-upload.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise HubError('upload_busy','Another YouTube chunk is active; continue this same publication later',True,diagnostics={'mutation_attempted':False}) from None
        yield
    finally:os.close(fd)
