"""Private worker protocol tests use fixture copies, never FFmpeg encoding."""
import hashlib
import http.client
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

from app.services.normalization_protocol import request_signature, response_signature, ffmpeg_args
from normalizer import server

SECRET = 'private-test-worker-secret-' * 3
BODY = Path('tests/fixtures/reel-h264-aac-edit-list-5s.mp4').read_bytes()
OUTPUT = Path('tests/fixtures/reel-h264-aac-5s.mp4').read_bytes()


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    # Exercise the same reservation logic with a tiny test-only job ceiling;
    # fixtures are 11 KiB and do not need the production 100 MiB allowance.
    monkeypatch.setattr(server, 'MAX_BYTES', 1024 * 1024)
    def encode(args, **kwargs):
        assert args[args.index('-c:v') + 1] == 'copy'
        assert kwargs['timeout'] == 90
        Path(args[-1]).write_bytes(OUTPUT)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(server.subprocess, 'run', encode)
    # Bind loopback only; no real provider calls or encoder in this test.
    instance = server.WorkerServer(('127.0.0.1', 0), server.Handler)
    instance.secret = SECRET
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance.server_port, tmp_path
    instance.shutdown();thread.join(timeout=3);instance.server_close()


def request(port, *, overrides=None, body=BODY, job='a'*32):
    digest = hashlib.sha256(body).hexdigest();expiry = str(int(time.time()) + 60)
    headers = {'Content-Length':str(len(body)), 'X-Job':job, 'X-Expires':expiry,
        'X-Source-Sha256':digest, 'X-Signature':request_signature(SECRET,job,expiry,digest,len(body))}
    if overrides:headers.update(overrides)
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
    connection.request('POST','/v1/normalize',body,headers)
    response=connection.getresponse();data=response.read();out=(response.status,dict(response.getheaders()),data)
    connection.close()
    # The zero-length error response can reach the client just before the
    # handler's finally block deletes scratch. A second serial request is a
    # deterministic completion barrier, without sleep-based cleanup assertions.
    barrier=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
    barrier.request('GET','/healthz');ready=barrier.getresponse()
    assert ready.status==200
    ready.read();barrier.close()
    return out


def test_authentication_digest_signed_result_and_replay(worker):
    port,root=worker
    status,headers,body=request(port)
    assert status==200 and body==OUTPUT
    digest=hashlib.sha256(body).hexdigest()
    assert headers['X-Signature']==response_signature(SECRET,'a'*32,digest,len(body))
    assert request(port)[0]==409
    assert not list(root.glob('.job-*')) and len(list(root.glob('.used-*')))==1


@pytest.mark.parametrize('headers',[
    {'X-Signature':'0'*64},{'X-Job':'../bad'}, {'X-Source-Sha256':'not-a-hash'},
    {'X-Expires':'1000000000'}, {'Content-Length':str(101*1024*1024)},
    {'Transfer-Encoding':'chunked'}])
def test_rejected_requests_do_not_create_jobs(worker,headers):
    port,root=worker
    assert request(port,overrides=headers)[0] in {400,403,413}
    assert not list(root.iterdir())


def test_worker_rejects_payload_that_does_not_match_grant(worker):
    port,root=worker
    expiry=str(int(time.time())+60);digest='0'*64
    headers={'X-Source-Sha256':digest,'X-Expires':expiry,
        'X-Signature':request_signature(SECRET,'a'*32,expiry,digest,len(BODY))}
    assert request(port,overrides=headers)[0]==422
    assert not list(root.glob('.job-*'))


def test_stale_cleanup_only_touches_worker_owned_names(tmp_path):
    import os
    source=tmp_path/'.job-old';source.mkdir();(source/'input.mp4').write_bytes(b'private')
    marker=tmp_path/('.used-'+'a'*32);marker.write_bytes(b'')
    unrelated=tmp_path/'keep';unrelated.write_bytes(b'keep')
    old=time.time()-1000
    os.utime(source,(old,old));os.utime(marker,(old,old));os.utime(unrelated,(old,old))
    server.cleanup(tmp_path)
    assert not source.exists() and not marker.exists() and unrelated.read_bytes()==b'keep'


def test_insufficient_worker_disk_space_rejects_before_conversion(worker,monkeypatch):
    from types import SimpleNamespace
    port,root=worker
    monkeypatch.setattr(server.shutil,'disk_usage',lambda p:SimpleNamespace(free=0))
    assert request(port)[0]==507
    assert not list(root.glob('.job-*'))


def test_profile_never_decodes_or_encodes_video():
    args=ffmpeg_args('/private/input.mp4','/private/output.mp4')
    assert args[args.index('-c:v')+1]=='copy'
    assert args[args.index('-b:a')+1]=='120k'
    assert args[args.index('-use_editlist')+1]=='0'
    assert args[args.index('-protocol_whitelist')+1]=='file,pipe'
    assert '-n' in args
