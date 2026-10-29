"""Single-job private conversion worker. No database, Meta or publishing access."""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import tempfile
import time

from app.services.normalization_protocol import MAX_BYTES, ffmpeg_args, request_signature, response_signature, valid_digest

ROOT = Path('/work')


def cleanup(root=None):
    root = ROOT if root is None else root
    for path in root.iterdir():
        if (not re.fullmatch(r'\.(?:job|used)-[a-zA-Z0-9_\-]+', path.name)
                or path.is_symlink() or time.time() - path.stat().st_mtime < 600):
            continue
        if path.is_dir(): shutil.rmtree(path)
        elif path.is_file(): path.unlink()


def limits():
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES, MAX_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class Handler(BaseHTTPRequestHandler):
    server_version = 'PrivateMediaWorker'

    def log_message(self, *args):
        pass  # No request headers, paths, signatures or media are logged.

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def reply(self, status):
        self.send_response(status)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def do_GET(self):
        self.reply(200 if self.path == '/healthz' else 404)

    def do_POST(self):
        if self.path != '/v1/normalize':
            self.reply(404); return
        try:
            required = ['Content-Length', 'X-Job', 'X-Expires', 'X-Source-Sha256', 'X-Signature']
            if self.headers.get('Transfer-Encoding') or any(len(self.headers.get_all(k, [])) != 1 for k in required):
                self.reply(400); return
            job, expires, digest = (self.headers[k] for k in ['X-Job', 'X-Expires', 'X-Source-Sha256'])
            if (not re.fullmatch('[a-f0-9]{32}', job) or not re.fullmatch('[0-9]{10}', expires)
                    or not valid_digest(digest) or not re.fullmatch('[0-9]{1,9}', self.headers['Content-Length'])):
                self.reply(400); return
            size = int(self.headers['Content-Length'])
            if not 0 < size <= MAX_BYTES:
                self.reply(413); return
            expected = request_signature(self.server.secret, job, expires, digest, size)
            if not time.time() < int(expires) <= time.time() + 120 or not hmac.compare_digest(expected, self.headers['X-Signature']):
                self.reply(403); return
            cleanup()
            try:
                marker = os.open(ROOT / ('.used-' + job), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(marker)
            except FileExistsError:
                self.reply(409); return
            if shutil.disk_usage(ROOT).free < 2 * MAX_BYTES + 64 * 1024 * 1024:
                self.reply(507); return
            with tempfile.TemporaryDirectory(prefix='.job-', dir=ROOT) as temporary:
                source, output = Path(temporary) / 'input.mp4', Path(temporary) / 'output.mp4'
                remaining = size; checksum = hashlib.sha256(); deadline = time.monotonic() + 90
                with source.open('xb') as target:
                    while remaining:
                        if time.monotonic() >= deadline:raise TimeoutError()
                        data = self.rfile.read1(min(65536, remaining))
                        if not data:raise ValueError()
                        target.write(data); checksum.update(data); remaining -= len(data)
                if checksum.hexdigest() != digest:
                    self.reply(422); return
                subprocess.run(ffmpeg_args(source, output), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=90, check=True, preexec_fn=limits)
                length = output.stat().st_size
                if not 0 < length <= MAX_BYTES:raise ValueError()
                with output.open('rb') as stream:
                    result_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(length))
                self.send_header('X-Output-Sha256', result_hash)
                self.send_header('X-Signature', response_signature(self.server.secret, job, result_hash, length))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                deadline = time.monotonic() + 90
                with output.open('rb') as stream:
                    while block := stream.read(65536):
                        if time.monotonic() >= deadline:raise TimeoutError()
                        self.wfile.write(block)
        except (OSError, ValueError, subprocess.SubprocessError):
            # A disconnected client cannot leave a durable output or running encoder.
            try:self.reply(422)
            except OSError:pass


class WorkerServer(HTTPServer):
    last_cleanup = 0

    def service_actions(self):
        if time.monotonic() - self.last_cleanup >= 60:
            cleanup()
            self.last_cleanup = time.monotonic()


def main():
    os.umask(0o077)
    secret = os.environ.get('MEDIA_NORMALIZATION_SECRET', '')
    if len(secret) < 64 or os.getuid() == 0:
        raise SystemExit('Private worker requires non-root execution and its dedicated secret')
    if ROOT.is_symlink():raise SystemExit('Private worker storage is unavailable')
    ROOT.mkdir(mode=0o700, exist_ok=True)
    ROOT.chmod(0o700);cleanup()
    server = WorkerServer(('0.0.0.0', 8090), Handler)
    server.secret = secret
    server.serve_forever(poll_interval=1)


if __name__ == '__main__':
    main()
