"""Metadata-only benchmark. Report CPU and observed process RSS; never encode media."""
import argparse
import json
from pathlib import Path
import platform
import resource
import shutil
import statistics
import subprocess
import time

p=argparse.ArgumentParser()
p.add_argument('media_path',type=Path)
p.add_argument('--repetitions',type=int,default=20)
a=p.parse_args()
if not a.media_path.is_file() or not 1<=a.repetitions<=100:
    raise SystemExit('Select a local media fixture and 1–100 repetitions')
probe=shutil.which('ffprobe')
if not probe:raise SystemExit('ffprobe is not installed')
from app.services.media_manager import probe_command
command=probe_command(a.media_path)
wall_times=[];cpu_times=[];peaks=[]
for _ in range(a.repetitions):
    before=resource.getrusage(resource.RUSAGE_CHILDREN)
    start=time.perf_counter()
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    observed_peak=0
    while process.poll() is None:
        try:
            # Popen returns after exec. Restrict measurement to ffprobe, not a pre-exec Python fork.
            if Path(f'/proc/{process.pid}/comm').read_text().strip()=='ffprobe':
                for line in Path(f'/proc/{process.pid}/status').read_text().splitlines():
                    if line.startswith(('VmHWM:','VmRSS:')):
                        observed_peak=max(observed_peak,int(line.split()[1]))
        except (FileNotFoundError,ProcessLookupError):
            pass
        time.sleep(0.001)
    output,error=process.communicate()
    elapsed=time.perf_counter()-start
    after=resource.getrusage(resource.RUSAGE_CHILDREN)
    if process.returncode!=0:raise SystemExit('Metadata probe failed; no raw provider/file details displayed')
    metadata=json.loads(output)
    wall_times.append(elapsed)
    cpu_times.append(after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime)
    if observed_peak:peaks.append(observed_peak/1024)
print(json.dumps({'architecture':platform.machine(),'ffprobe_version':subprocess.check_output([probe,'-version'],text=True).splitlines()[0],
    'fixture_bytes':a.media_path.stat().st_size,'fixture_duration_seconds':float(metadata['format']['duration']),
    'repetitions':a.repetitions,'operation':'local-file metadata only; no transcoding',
    'median_wall_ms':round(statistics.median(wall_times)*1000,3),
    'median_cpu_ms':round(statistics.median(cpu_times)*1000,3),
    'average_cpu_percent_one_core':round(sum(cpu_times)/sum(wall_times)*100,2),
    'observed_peak_probe_rss_mib':round(max(peaks),3) if peaks else None,
    'rss_sampling_interval_ms':1,'encoders_installed':bool(shutil.which('ffmpeg'))},indent=2))
