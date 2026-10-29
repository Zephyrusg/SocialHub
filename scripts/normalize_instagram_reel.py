"""Explicit, local-only derivative creation. Never imports or publishes media.

Run with the project environment from the repository root. FFmpeg runs only in
a pinned, networkless development container; the production runtime is unchanged.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

from app.config import Settings
from app.core import HubError
from app.connectors.reels import ReelWorkflows
from app.services.media_manager import MediaManager
from app.services.video_fingerprint import video_fingerprint


IMAGE = 'mwader/static-ffmpeg:9.0.2-1@sha256:f5d100184884eb3e82b0eb14bb5ebf98da99826ac824989720297fb9ef21c822'
MAX_BYTES = 100 * 1024 * 1024


def command(source, destination):
    return ['docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
            '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--user', f'{os.getuid()}:{os.getgid()}', '--memory', '384m',
            '--cpus', '1', '--pids-limit', '64', '--ulimit', f'fsize={MAX_BYTES}:{MAX_BYTES}',
            '--entrypoint', '/ffmpeg',
            '--mount', f'type=bind,src={source},dst=/input.mp4,readonly',
            '--mount', f'type=bind,src={destination},dst=/output', IMAGE,
            '-nostdin', '-hide_banner', '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-i', '/input.mp4', '-map', '0:v:0', '-map', '0:a:0?',
            '-c:v', 'copy', '-c:a', 'aac', '-b:a', '120k', '-ar', '48000', '-ac', '2',
            '-map_metadata', '-1', '-map_chapters', '-1', '-movflags', '+faststart',
            '-use_editlist', '0', '-n', '/output/normalized.mp4']


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_output(manager, before, path, expected_video):
    mime, metadata = manager.inspect(path)
    ReelWorkflows.validate_reel(SimpleNamespace(platform='instagram'),
                               {'mime_type': mime, 'metadata': metadata})
    if any(metadata[key] != before[key] for key in ('width', 'height', 'frame_rate', 'video_codec')):
        raise ValueError('Video properties changed unexpectedly')
    # No-edit-list MP4 may expose B-frame delay and AAC encoder priming.
    if abs(metadata['duration'] - before['duration']) > 0.25:
        raise ValueError('Unexpected duration change; inspect source timing manually')
    if video_fingerprint(path) != expected_video:
        raise ValueError('Compressed video changed; output was rejected')
    return metadata


def normalize(source, output):
    """Return a verified manifest; fail closed without replacing existing files."""
    source, output = Path(source), Path(output)
    if os.getuid() == 0:
        raise ValueError('Run this local helper as a non-root user with Docker access')
    if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= MAX_BYTES:
        raise ValueError('Select a regular local MP4 of at most 100 MiB')
    if os.path.lexists(output) or not output.parent.is_dir():
        raise ValueError('Output must be new and its parent directory must already exist')
    if not shutil.which('ffprobe'):
        raise ValueError('Put the prebuilt ffprobe on PATH before running this helper')
    output = output.absolute()
    # A private snapshot prevents accidental mutation of the original or its live
    # publication record. Both scratch and output remain outside the container.
    with tempfile.TemporaryDirectory(prefix='.normalize-', dir=output.parent) as temporary:
        scratch = Path(temporary)
        snapshot = scratch / 'source.mp4'
        with source.open('rb') as src, snapshot.open('xb') as dst:
            remaining = MAX_BYTES + 1
            while remaining and (chunk := src.read(min(1024 * 1024, remaining))):
                dst.write(chunk)
                remaining -= len(chunk)
        snapshot.chmod(0o600)
        if snapshot.stat().st_size > MAX_BYTES:
            raise ValueError('Source grew beyond the size limit')
        settings = Settings(_env_file=None, database_url='sqlite://',
                            encryption_key='unused-local-inspection', local_token='x' * 64,
                            media_root=str(scratch), media_max_bytes=MAX_BYTES)
        manager = MediaManager(settings, None)  # inspect only; never opens a database.
        mime, before = manager.inspect(snapshot)
        if mime != 'video/mp4':
            raise ValueError('Only MP4 normalization is supported')
        expected_video = video_fingerprint(snapshot)
        destination = scratch / 'output'
        destination.mkdir(mode=0o700)
        # Docker --rm cleans up on completion; on a timeout explicitly remove the
        # named container so encoding cannot continue unattended.
        name = 'socialhub-normalize-' + os.urandom(8).hex()
        args = command(snapshot, destination)
        args[2:2] = ['--name', name]
        try:
            with tempfile.TemporaryFile() as errors:
                subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=errors, timeout=120, check=True)
        except (subprocess.SubprocessError, OSError):
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, check=False)
            raise ValueError('Local conversion failed; no output was accepted') from None
        candidate = destination / 'normalized.mp4'
        metadata = validate_output(manager, before, candidate, expected_video)
        candidate.chmod(0o600)
        report = {'source_sha256': digest(snapshot), 'output_sha256': digest(candidate),
                  'size': candidate.stat().st_size, 'metadata': metadata,
                  'video_stream_unchanged': True, 'video_fingerprint': expected_video,
                  'audio_target_bps': 120000, 'instagram_validator': 'passed',
                  'imported': False, 'published': False}
        # Atomic no-clobber handoff on the same filesystem; never mutate source.
        os.link(candidate, output)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        report = normalize(args.source, args.output)
    except HubError as error:
        raise SystemExit(f'Normalization rejected ({error.code}): {error.message}') from None
    except (ValueError, OSError, subprocess.SubprocessError):
        raise SystemExit('Normalization failed; verify input, output, local tools and Instagram media requirements') from None
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
