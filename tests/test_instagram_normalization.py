"""Local derivative checks; normal test execution never launches an encoder."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import shutil

import pytest

from app.config import Settings
from app.core import HubError
from app.services.media_manager import MediaManager
from scripts import normalize_instagram_reel as normalization


def metadata():
    return {'width': 720, 'height': 1280, 'duration': 10.0, 'video_codec': 'h264',
            'audio_codecs': ['aac'], 'frame_rate': 24.0, 'pixel_format': 'yuv420p',
            'field_order': 'progressive', 'video_bit_rate': 3511060,
            'audio_details': [{'sample_rate': 48000, 'channels': 2, 'bit_rate': 121420}],
            'fast_start': True, 'has_edit_list': False}


def test_encoder_is_explicit_pinned_offline_and_copies_video(tmp_path):
    args = normalization.command(tmp_path / 'source.mp4', tmp_path / 'output')
    assert args[:6] == ['docker', 'run', '--rm', '--pull', 'never', '--network']
    assert args[args.index('--network') + 1] == 'none'
    assert '@sha256:' in normalization.IMAGE
    assert args[args.index('-c:v') + 1] == 'copy'
    assert args[args.index('-b:a') + 1] == '120k'
    assert args[args.index('-use_editlist') + 1] == '0'
    assert 'readonly' in args[args.index('--mount') + 1]
    assert '--read-only' in args and '--cap-drop' in args and '-n' in args


@pytest.mark.parametrize('case', ['existing', 'symlink', 'missing', 'empty'])
def test_local_helper_rejects_unsafe_paths_before_starting_tools(tmp_path, monkeypatch, case):
    source, output = tmp_path / 'input.mp4', tmp_path / 'out.mp4'
    source.write_bytes(b'original')
    if case == 'existing':
        output.write_bytes(b'keep')
    elif case == 'symlink':
        link = tmp_path / 'link.mp4'
        link.symlink_to(source)
        source = link
    elif case == 'missing':
        source = tmp_path / 'absent'
    else:
        source.write_bytes(b'')
    def forbidden(*args, **kwargs):
        pytest.fail('No subprocess should run for rejected paths')
    monkeypatch.setattr(normalization.subprocess, 'run', forbidden)
    with pytest.raises(ValueError):
        normalization.normalize(source, output)
    if case == 'existing':
        assert output.read_bytes() == b'keep'


@pytest.mark.parametrize('field,value', [('has_edit_list', True), ('audio_bps', 128001),
                                       ('duration', 11), ('width', 1080), ('video_hash', 'changed')])
def test_derivative_must_pass_real_validator_and_preserve_video(monkeypatch, field, value):
    before = metadata()
    after = deepcopy(before)
    if field == 'audio_bps':
        after['audio_details'][0]['bit_rate'] = value
    elif field != 'video_hash':
        after[field] = value
    fingerprint = {'packets': 240, 'sha256': 'original'}
    monkeypatch.setattr(normalization, 'video_fingerprint', lambda p:
                        fingerprint if field != 'video_hash' else {'packets': 240, 'sha256': value})
    manager = SimpleNamespace(inspect=lambda p: ('video/mp4', after))
    with pytest.raises((ValueError, HubError)):
        normalization.validate_output(manager, before, Path('output.mp4'), fingerprint)


def test_current_flow_failure_remains_rejected_and_valid_derivative_passes(monkeypatch):
    before = metadata()
    before['audio_details'][0]['bit_rate'] = 255256
    before['has_edit_list'] = True
    fingerprint = {'packets': 240, 'sha256': 'same-video'}
    monkeypatch.setattr(normalization, 'video_fingerprint', lambda p: fingerprint)
    manager = SimpleNamespace(inspect=lambda p: ('video/mp4', metadata()))
    result = normalization.validate_output(manager, before, Path('output.mp4'), fingerprint)
    assert result['audio_details'][0]['bit_rate'] < 128000
    assert before['has_edit_list'] is True  # Input metadata is immutable.
    with pytest.raises(HubError):
        normalization.ReelWorkflows.validate_reel(SimpleNamespace(platform='instagram'),
            {'mime_type': 'video/mp4', 'metadata': before})


@pytest.mark.skipif(not shutil.which('ffprobe'), reason='Requires the prebuilt metadata probe')
def test_pregenerated_fixture_and_packet_fingerprint_without_encoding(tmp_path):
    settings = Settings(_env_file=None, database_url='sqlite://', encryption_key='unused',
                        local_token='x' * 64, media_root=str(tmp_path))
    manager = MediaManager(settings, None)
    fixture = Path('tests/fixtures/reel-h264-aac-5s.mp4')
    _, before = manager.inspect(fixture)
    fingerprint = normalization.video_fingerprint(fixture)
    result = normalization.validate_output(manager, before, fixture, fingerprint)
    assert fingerprint['packets'] > 0
    assert result['has_edit_list'] is False
