"""Fixed conversion profile and authenticated private worker wire protocol."""
import hashlib
import hmac
import re

PROFILE = 'instagram-reel-v1'
MAX_BYTES = 100 * 1024 * 1024
WORKER_URL = 'http://normalizer:8090/v1/normalize'


def signature(secret, *parts):
    return hmac.new(secret.encode(), '\n'.join(str(p) for p in parts).encode(), hashlib.sha256).hexdigest()


def request_signature(secret, job, expires, digest, size):
    return signature(secret, 'request', PROFILE, job, expires, digest, size)


def response_signature(secret, job, digest, size):
    return signature(secret, 'response', PROFILE, job, digest, size)


def valid_digest(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def ffmpeg_args(source, output):
    return ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error',
            '-protocol_whitelist', 'file,pipe', '-i', str(source),
            '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'copy', '-c:a', 'aac',
            '-b:a', '120k', '-ar', '48000', '-ac', '2', '-threads', '1',
            '-map_metadata', '-1', '-map_chapters', '-1', '-movflags', '+faststart',
            '-use_editlist', '0', '-n', str(output)]
