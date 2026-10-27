import time
from types import SimpleNamespace
import jwt
import pytest
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric import rsa
from app.config import Settings
from app.security.auth import Verifier
from app.security.tokens import TokenVault
from app.services.media import MediaStore
from app.core import HubError


def settings(**kw):
    return Settings(_env_file=None, database_url='sqlite://', encryption_key=Fernet.generate_key().decode(), local_token='a' * 64, **kw)


async def test_local_auth():
    verifier = Verifier(settings())
    assert await verifier.verify_token('wrong') is None
    assert (await verifier.verify_token('a' * 64)).client_id == 'local-developer'


async def test_oauth_claims(monkeypatch):
    s = settings(oauth_allowed_subjects=['alice'], oauth_allowed_client_ids=['test-client'], auth_mode='oauth', oauth_issuer='https://issuer.example/', oauth_jwks_url='https://issuer.example/keys', oauth_audience='https://socialhub.example/mcp', public_url='https://socialhub.example/mcp')
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = Verifier(s)
    monkeypatch.setattr(verifier.jwks, 'get_signing_key_from_jwt', lambda token: SimpleNamespace(key=key.public_key()))
    claims = {'iss': s.oauth_issuer, 'aud': s.oauth_audience, 'sub': 'alice', 'iat': int(time.time()), 'exp': int(time.time()) + 60, 'scope': 'socialhub:read', 'client_id': 'test-client'}
    token = jwt.encode(claims, key, algorithm='RS256', headers={'typ': 'at+jwt'})
    assert (await verifier.verify_token(token)).client_id == 'alice'
    for changes in [{'aud': 'wrong'}, {'iss': 'https://evil.example'}, {'exp': 1}, {'sub': None}]:
        assert await verifier.verify_token(jwt.encode(claims | changes, key, algorithm='RS256', headers={'typ': 'at+jwt'})) is None
    assert await verifier.verify_token(jwt.encode(claims, 'secret' * 8, algorithm='HS256')) is None


def test_bad_local_secret():
    with pytest.raises(ValueError):
        Settings(_env_file=None, database_url='sqlite://', encryption_key='x', local_token='short')


def test_credential_encryption_and_expiry():
    old, new = Fernet.generate_key(), Fernet.generate_key()
    vault = TokenVault([old])
    ciphertext = vault.encrypt({'access_token': 'sensitive'})
    credential = SimpleNamespace(ciphertext=ciphertext, expires_at=None, revoked=False)
    assert 'sensitive' not in ciphertext
    assert TokenVault([new, old]).decrypt(credential)['access_token'] == 'sensitive'
    with pytest.raises(InvalidToken):
        TokenVault([new]).decrypt(credential)
    credential.revoked = True
    with pytest.raises(HubError):
        vault.decrypt(credential)


def test_media_private_and_deduplicated(sessions, tmp_path):
    store = MediaStore(tmp_path / 'media', sessions)
    data = b'\x89PNG\r\n\x1a\n' + b'mock-data'
    mid = store.ingest(data, 'image/png')
    assert store.ingest(data, 'image/png') == mid
    path = tmp_path / 'media' / mid
    assert path.read_bytes() == data
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(list((tmp_path / 'media').iterdir())) == 1


@pytest.mark.parametrize('data,mime', [(b'bad', 'image/png'), (b'', 'image/png'), (b'abc', 'text/plain')])
def test_invalid_media(sessions, tmp_path, data, mime):
    with pytest.raises(HubError):
        MediaStore(tmp_path, sessions).ingest(data, mime)
