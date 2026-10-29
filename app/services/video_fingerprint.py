"""Bounded compressed-video identity checks; no video encoding or decoding."""
import hashlib
import json
import subprocess
import tempfile


def video_fingerprint(path):
    with tempfile.TemporaryFile() as data:
        subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                        '-select_streams', 'v:0', '-show_packets', '-show_data_hash', 'sha256',
                        '-show_entries', 'packet=data_hash', '-of', 'json', str(path)],
                       stdin=subprocess.DEVNULL, stdout=data, stderr=subprocess.DEVNULL,
                       timeout=30, check=True)
        data.seek(0)
        raw = data.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('Video packet report exceeds verification limit')
    hashes = [packet['data_hash'] for packet in json.loads(raw)['packets']]
    if not hashes or any(not h.startswith('SHA256:') or len(h) != 71 for h in hashes):
        raise ValueError('Missing compressed video hashes')
    return {'packets': len(hashes), 'sha256': hashlib.sha256(''.join(hashes).encode()).hexdigest()}
