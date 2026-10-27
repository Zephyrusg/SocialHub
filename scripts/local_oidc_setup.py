"""Generate disposable WSL-only identity data under ignored secrets/; no values printed."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import os
import secrets
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'secrets/local-oidc'

def write(name, value):
    path = DEST / name
    path.write_text(value)
    path.chmod(0o600)

def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)

def pem(k):
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()

def hash_secret(value):
    salt=secrets.token_bytes(16)
    b64=lambda b:base64.b64encode(b).rstrip(b'=').decode().replace('+', '.')
    return '$pbkdf2-sha512$310000$'+b64(salt)+'$'+b64(hashlib.pbkdf2_hmac('sha512',value.encode(),salt,310000))

def main():
    os.umask(0o077)
    DEST.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (DEST/'configuration.yml').exists():
        print('Local OIDC configuration already exists; retained.')
        return
    tls = key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'SocialHub disposable local test CA')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(tls.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('auth.socialhub.test'), x509.DNSName('hub.socialhub.test')]), critical=False)
        .sign(tls, hashes.SHA256()))
    write('tls.key',pem(tls)); write('ca.crt',cert.public_bytes(serialization.Encoding.PEM).decode())
    # Public CA must be readable by the nonroot app through its single-file bind.
    # The containing directory and every private key/secret remain protected.
    (DEST/'ca.crt').chmod(0o644)
    oidc_key = pem(key()); write('signing.key',oidc_key)
    password = secrets.token_urlsafe(32); portal_secret=secrets.token_urlsafe(48)
    # Authelia supports PBKDF2-SHA512; avoid an extra password-hashing runtime dependency.
    salt=secrets.token_bytes(16); iterations=310000
    b64=lambda b:base64.b64encode(b).rstrip(b'=').decode().replace('+', '.')
    hashed=f'$pbkdf2-sha512${iterations}${b64(salt)}${b64(hashlib.pbkdf2_hmac("sha512",password.encode(),salt,iterations))}'
    users={'users':{u:{'displayname':u,'password':hashed,'email':u+'@socialhub.test','groups':['testers']} for u in ('owner','outsider')}}
    write('users.yml',yaml.safe_dump(users)); write('test-credentials.json',json.dumps({'password':password,'portal_secret':portal_secret}))
    resource='https://socialhub.example.test/mcp'
    config={
      'server':{'address':'tcp://:9091/'},'log':{'level':'error'},
      'authentication_backend':{'file':{'path':'/config/users.yml','watch':False}},
      'access_control':{'default_policy':'deny','rules':[{'domain':'hub.socialhub.test','policy':'one_factor'}]},
      'session':{'secret':secrets.token_hex(64),'cookies':[{'domain':'socialhub.test','authelia_url':'https://auth.socialhub.test:9443'}]},
      'storage':{'encryption_key':secrets.token_hex(32),'local':{'path':'/config/db.sqlite3'}},
      'notifier':{'filesystem':{'filename':'/config/notifications.txt'}},
      'identity_validation':{'reset_password':{'jwt_secret':secrets.token_hex(64)}},
      'identity_providers':{'oidc':{
        'hmac_secret':secrets.token_hex(64),
        'jwks':[{'key_id':'local-key-1','algorithm':'RS256','use':'sig','key':oidc_key}],
        'lifespans':{'access_token':'1m','id_token':'5m','authorize_code':'1m'},
        'scopes':{'socialhub:read':{'claims':[]}},
        'clients':[
          {'client_id':'socialhub-chatgpt-local','client_name':'SocialHub MCP local PKCE', 'public':True,
           'authorization_policy':'one_factor','redirect_uris':['https://hub.socialhub.test:9444/test/callback'],
           'scopes':['openid','socialhub:read'],'audience':[resource],'requested_audience_mode':'explicit',
           'grant_types':['authorization_code'],'response_types':['code'],'token_endpoint_auth_method':'none',
           'require_pkce':True,'pkce_challenge_method':'S256','access_token_signed_response_alg':'RS256'},
          {'client_id':'socialhub-owner-local','client_name':'SocialHub independent owner','client_secret':hash_secret(portal_secret),
           'public':False,'authorization_policy':'one_factor',
           'redirect_uris':['https://hub.socialhub.test:9444/approval/oidc/callback'],
           'scopes':['openid'],'grant_types':['authorization_code'],'response_types':['code'],
           'token_endpoint_auth_method':'client_secret_basic','require_pkce':True,'pkce_challenge_method':'S256'}]}}}
    write('configuration.yml',yaml.safe_dump(config,sort_keys=False))
    write('socialhub.env','\n'.join([
      'DATABASE_URL=sqlite:////data/test.db','ENCRYPTION_KEY='+Fernet.generate_key().decode(),
      'AUTH_MODE=oauth','PUBLIC_URL='+resource,'OAUTH_AUDIENCE='+resource,
      'OAUTH_ISSUER=https://auth.socialhub.test:9443','OAUTH_JWKS_URL=https://auth.socialhub.test:9443/jwks.json',
      'BROWSER_ORIGIN=https://hub.socialhub.test:9444','META_REDIRECT_URI=https://hub.socialhub.test:9444/meta/oauth/callback',
      'TRUSTED_PROXY_CIDRS=["172.30.83.10/32"]','OAUTH_ALLOWED_SUBJECTS=[]',
      'OAUTH_ALLOWED_CLIENT_IDS=["socialhub-chatgpt-local"]','APPROVAL_PORTAL_ENABLED=true',
      'OWNER_OIDC_ENABLED=true','OWNER_OIDC_CLIENT_ID=socialhub-owner-local','OWNER_OIDC_CLIENT_SECRET='+portal_secret,
      'OWNER_OIDC_ALLOWED_SUBJECTS=["not-bound-yet"]','OWNER_OIDC_REQUIRE_MFA=false',
      'MEDIA_DELIVERY_ENABLED=false','MEDIA_ROOT=/data/media','META_LIVE_READS=false','META_LIVE_WRITES=false',
      'PUBLISHING_ENABLED=false','APPROVAL_MODE=owner_required','CHATGPT_CONFIRMATION_TESTED=false',
      'CHATGPT_RESIDUAL_RISK_ACCEPTED=false','SSL_CERT_FILE=/local-ca/ca.crt'])+'\n')
    print('Disposable local OIDC configuration generated; credentials remain in ignored secrets/local-oidc/.')

if __name__=='__main__':main()
