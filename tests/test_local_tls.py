import subprocess
import sys
from pathlib import Path
from cryptography import x509
from cryptography.x509.verification import PolicyBuilder, Store


def test_local_certificates_strict_validation_and_safe_repair(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    helper = scripts / 'local_tls.py'
    helper.write_bytes(Path('scripts/local_tls.py').read_bytes())
    subprocess.run([sys.executable, str(helper)], check=True, capture_output=True)
    tls = tmp_path / 'secrets' / 'tls'
    key_before = (tls / 'ca.key').read_bytes()
    ca = x509.load_pem_x509_certificate((tls / 'ca.crt').read_bytes())
    leaf = x509.load_pem_x509_certificate((tls / 'localhost.crt').read_bytes())
    PolicyBuilder().store(Store([ca])).build_server_verifier(x509.DNSName('localhost')).verify(leaf, [])
    assert (tls / 'ca.key').stat().st_mode & 0o777 == 0o600
    assert (tls / 'localhost.key').stat().st_mode & 0o777 == 0o600
    refused = subprocess.run([sys.executable, str(helper)], capture_output=True)
    assert refused.returncode != 0
    subprocess.run([sys.executable, str(helper), '--repair-extensions'], check=True, capture_output=True)
    assert (tls / 'ca.key').read_bytes() == key_before
    assert (tls / 'ca.crt.before-extensions').exists()
    ca = x509.load_pem_x509_certificate((tls / 'ca.crt').read_bytes())
    leaf = x509.load_pem_x509_certificate((tls / 'localhost.crt').read_bytes())
    PolicyBuilder().store(Store([ca])).build_server_verifier(x509.DNSName('localhost')).verify(leaf, [])
