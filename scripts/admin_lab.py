"""Prepare an isolated synthetic admin OIDC lab; never SSH or contact Meta."""
import json,sys,subprocess,secrets
from pathlib import Path
import yaml
import local_oidc_setup as fixture
import local_oidc_totp as totp
ROOT=Path(__file__).resolve().parents[1];DEST=ROOT/'secrets/admin-lab'
COMPOSE=['docker','compose','--project-name','socialhub-admin-lab','-f',str(ROOT/'deployment/admin-lab/compose.yml')]

def run(args):
    r=subprocess.run(args,capture_output=True)
    if r.returncode:raise SystemExit('Admin lab command failed; sensitive tool diagnostics suppressed')

def main():
    action=sys.argv[1] if len(sys.argv)>1 else 'prepare'
    if action=='down':run(COMPOSE+['down']);return
    if action=='enroll':
        totp.DEST=DEST;totp.COMPOSE=COMPOSE;totp.main();return
    if action!='prepare':raise SystemExit('Usage: admin_lab.py prepare|enroll|down')
    if (DEST/'admin-lab.json').exists():print('Existing isolated admin lab retained.');return
    if DEST.exists():raise SystemExit('Partial fixture exists; inspect privately before regenerating')
    fixture.DEST=DEST;fixture.main()
    creds=json.loads((DEST/'test-credentials.json').read_text());secret=secrets.token_urlsafe(48);creds['admin_client_secret']=secret
    (DEST/'test-credentials.json').write_text(json.dumps(creds));(DEST/'test-credentials.json').chmod(0o600)
    config=yaml.safe_load((DEST/'configuration.yml').read_text())
    config['session']['cookies'][0]['authelia_url']='https://auth.socialhub.test:9453'
    config['identity_providers']['oidc']['clients']=[{
        'client_id':'socialhub-admin-local','client_name':'Synthetic administration lab','public':False,
        'client_secret':fixture.hash_secret(secret),'authorization_policy':'two_factor',
        'redirect_uris':['https://hub.socialhub.test:9454/admin/oidc/callback'],
        'scopes':['openid'],'grant_types':['authorization_code'],'response_types':['code'],
        'token_endpoint_auth_method':'client_secret_basic','require_pkce':True,'pkce_challenge_method':'S256'}]
    (DEST/'configuration.yml').write_text(yaml.safe_dump(config,sort_keys=False))
    env=dict(line.split('=',1) for line in (DEST/'socialhub.env').read_text().splitlines() if '=' in line)
    origin='https://hub.socialhub.test:9454'
    env.update(PUBLIC_URL=origin+'/mcp',BROWSER_ORIGIN=origin,META_REDIRECT_URI=origin+'/meta/oauth/callback',
        OAUTH_AUDIENCE=origin+'/mcp',OAUTH_ISSUER='https://auth.socialhub.test:9453',OAUTH_JWKS_URL='https://auth.socialhub.test:9453/jwks.json',
        TRUSTED_PROXY_CIDRS='["172.30.85.10/32"]',ADMIN_ENABLED='true',ADMIN_OIDC_CLIENT_ID='socialhub-admin-local',
        ADMIN_OIDC_CLIENT_SECRET=secret,ADMIN_ALLOWED_SUBJECTS='["not-bound-yet"]',PUBLISHING_ALLOWED_SUBJECTS='["not-bound-yet"]',
        APPROVAL_PORTAL_ENABLED='false',OWNER_OIDC_ENABLED='false',OWNER_OIDC_CLIENT_ID='',OWNER_OIDC_CLIENT_SECRET='',
        OWNER_OIDC_ALLOWED_SUBJECTS='[]',META_LIVE_READS='false',META_LIVE_WRITES='false',PUBLISHING_ENABLED='false')
    (DEST/'socialhub.env').write_text('\n'.join(k+'='+v for k,v in env.items())+'\n')
    (DEST/'socialhub.env').chmod(0o600);(DEST/'admin-lab.json').write_text('{"synthetic": true}\n');(DEST/'admin-lab.json').chmod(0o600)
    print('Isolated synthetic admin configuration generated privately. No runtime or production change.')
if __name__=='__main__':main()
