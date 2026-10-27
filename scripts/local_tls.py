"""Dedicated localhost development CA; never changes host/browser trust."""
import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

parser = argparse.ArgumentParser()
parser.add_argument('--repair-extensions', action='store_true', help='Preserve existing keys and back up/reissue certificates with strict X.509 extensions')
args = parser.parse_args()
secrets_dir = Path(__file__).resolve().parents[1] / 'secrets'
secrets_dir.mkdir(mode=0o700, exist_ok=True)
root = secrets_dir / 'tls'
root.mkdir(exist_ok=True, mode=0o700)
if any(root.iterdir()) and not args.repair_extensions:
    raise SystemExit('TLS directory is not empty; refusing to overwrite certificates')
if args.repair_extensions:
    ca_key = serialization.load_pem_private_key((root / 'ca.key').read_bytes(), password=None)
    key = serialization.load_pem_private_key((root / 'localhost.key').read_bytes(), password=None)
    # Back up only certificates; never duplicate private keys unnecessarily.
    for name in ['ca.crt', 'localhost.crt']:
        backup = root / (name + '.before-extensions')
        if not backup.exists():
            backup.write_bytes((root / name).read_bytes())
else:
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
now = datetime.now(timezone.utc)
ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'SocialHub local development CA')])
ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
      .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
      .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
      .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
      .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
      .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, key_encipherment=False,
         data_encipherment=False, key_agreement=False, content_commitment=False, encipher_only=False, decipher_only=False), critical=True).sign(ca_key, hashes.SHA256()))
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(ca_name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False, key_encipherment=True,
            data_encipherment=False, key_agreement=False, content_commitment=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False).sign(ca_key, hashes.SHA256()))
for filename, content, mode in [('ca.crt', ca.public_bytes(serialization.Encoding.PEM), 0o644),
    ('localhost.crt', cert.public_bytes(serialization.Encoding.PEM), 0o644),
    ('ca.key', ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), 0o600),
    ('localhost.key', key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), 0o600)]:
    if args.repair_extensions and filename.endswith('.key'):
        continue
    flags = os.O_WRONLY | (os.O_TRUNC if args.repair_extensions else os.O_CREAT | os.O_EXCL)
    fd = os.open(root / filename, flags, mode)
    with os.fdopen(fd, 'wb') as out:
        out.write(content)
print('Local certificates ready. Import only ca.crt after verifying this fingerprint; never share private keys.')
print('CA SHA-256 fingerprint:', ca.fingerprint(hashes.SHA256()).hex(':'))
