"""Bounded metadata-only ffprobe worker; no encoding or network protocols."""
import os
import resource
import sys


def probe_command(path):
    return ['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-f', 'mov',
            '-enable_drefs', '0', '-use_absolute_path', '0', '-threads', '1',
            '-max_alloc', '16777216', '-probesize', '5000000', '-analyzeduration', '5000000',
            '-show_entries', 'stream=codec_type,codec_name,width,height:format=duration,format_name',
            '-of', 'json', str(path)]


if __name__ == '__main__':
    # Use a worker rather than preexec_fn, which is unsafe in a threaded server.
    resource.setrlimit(resource.RLIMIT_DATA, (128 * 1024**2, 128 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024, 128 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    os.execvp('ffprobe', probe_command(sys.argv[1]))
