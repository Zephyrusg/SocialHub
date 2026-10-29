"""Real local Authelia password/TOTP -> standalone admin UI. No production/Meta requests."""
import asyncio,base64,hashlib,json,secrets,ssl,subprocess
from pathlib import Path
from urllib.parse import urlencode,urlsplit,parse_qs
import httpx,jwt
from playwright.async_api import async_playwright
import verify_local_oidc as helpers
from admin_lab import ROOT,DEST,COMPOSE
ISSUER='https://auth.socialhub.test:9453';ORIGIN='https://hub.socialhub.test:9454';CLIENT='socialhub-admin-local'
CHECKS={}

def command(args):
    r=subprocess.run(args,capture_output=True)
    if r.returncode:raise RuntimeError('Isolated lab operation failed')

async def bootstrap(page,http,metadata,credentials):
    verifier=secrets.token_urlsafe(48);nonce=secrets.token_urlsafe(32);state=secrets.token_urlsafe(32)
    callback=ORIGIN+'/admin/oidc/callback'
    query={'client_id':CLIENT,'response_type':'code','redirect_uri':callback,'scope':'openid','state':state,'nonce':nonce,
        'code_challenge':base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode(),'code_challenge_method':'S256','max_age':'900'}
    # The unbound synthetic callback is rejected by the app before token exchange.
    # Read its query privately and validate the signed identity before binding it.
    # This avoids Node route.fetch bypassing the browser's local DNS overrides.
    captured=await helpers.browser_finish(page,metadata['authorization_endpoint']+'?'+urlencode(query),
        urlsplit(callback).path,'owner',credentials['password'])
    assert captured.get('state')==[state] and captured.get('iss')==[ISSUER] and 'code' in captured
    r=await http.post(metadata['token_endpoint'],auth=(CLIENT,credentials['admin_client_secret']),data={
        'grant_type':'authorization_code','code':captured['code'][0],'redirect_uri':callback,'code_verifier':verifier})
    assert r.status_code==200
    token=r.json()['id_token'];header=jwt.get_unverified_header(token);keys=(await http.get(metadata['jwks_uri'])).json()['keys']
    key=next(k for k in keys if k['kid']==header['kid'])
    claims=jwt.decode(token,jwt.PyJWK.from_dict(key).key,algorithms=['RS256'],audience=CLIENT,issuer=ISSUER)
    assert claims['nonce']==nonce and {'pwd','otp'}<=set(claims['amr']) and isinstance(claims['auth_time'],int)
    path=DEST/'socialhub.env';s=path.read_text()
    for name in ['ADMIN_ALLOWED_SUBJECTS','PUBLISHING_ALLOWED_SUBJECTS']:
        s='\n'.join(name+'='+json.dumps([claims['sub']]) if x.startswith(name+'=') else x for x in s.splitlines())+'\n'
    path.write_text(s);path.chmod(0o600)
    command(COMPOSE+['up','-d','--no-deps','socialhub']);CHECKS['signed_synthetic_subject_binding']=True

async def main():
    helpers.SECRETS=DEST
    credentials=json.loads((DEST/'test-credentials.json').read_text())
    async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(DEST/'ca.crt')),trust_env=False,follow_redirects=False,timeout=20) as http:
        for _ in range(90):
            try:
                r=await http.get(ISSUER+'/.well-known/openid-configuration')
                if r.status_code==200:break
            except httpx.HTTPError:pass
            await asyncio.sleep(.5)
        assert r.status_code==200;metadata=r.json();assert metadata['issuer']==ISSUER
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(args=['--host-resolver-rules=MAP auth.socialhub.test 127.0.0.1,MAP hub.socialhub.test 127.0.0.1','--no-proxy-server'])
            context=await browser.new_context(ignore_https_errors=True)
            async def local_only(route):
                if urlsplit(route.request.url).hostname in {'auth.socialhub.test','hub.socialhub.test'}:await route.continue_()
                else:await route.abort()
            await context.route('**/*',local_only)
            page=await context.new_page();await bootstrap(page,http,metadata,credentials)
            for _ in range(60):
                r=await http.get(ORIGIN+'/readyz')
                if r.status_code==200:break
                await asyncio.sleep(.5)
            assert r.status_code==200
            await helpers.browser_finish(page,ORIGIN+'/admin/oidc/login','/admin/','owner',credentials['password'])
            assert urlsplit(page.url).path=='/admin/'
            text=await page.content();assert 'Publishing permission for this verified subject: yes' in text and 'Paused' in text
            assert credentials['password'] not in text and credentials['admin_client_secret'] not in text
            CHECKS['real_admin_code_pkce_mfa_login']=True;CHECKS['owner_has_both_roles']=True
            assert (await http.get(ORIGIN+'/approval/')).status_code==404;CHECKS['approval_portal_not_required']=True
            synthetic_secret=secrets.token_urlsafe(32)
            CHECKS['stage_setup_form']=True
            await page.goto(ORIGIN+'/admin/connections')
            await page.locator('input[name="app_id"]').fill('12345')
            await page.locator('input[name="app_secret"]').fill(synthetic_secret)
            CHECKS['stage_setup_filled']=True
            async with page.expect_response(lambda r: urlsplit(r.url).path=='/admin/meta/settings') as saved:
                await page.get_by_role('button',name='Save Meta settings',exact=True).click()
            saved_response=await saved.value
            CHECKS['setup_http_status']=saved_response.status
            CHECKS['setup_origin_matches_expected']=saved_response.request.headers.get('origin')==ORIGIN
            CHECKS['setup_origin_is_null']=saved_response.request.headers.get('origin')=='null'
            CHECKS['setup_result_path']=urlsplit(page.url).path
            print(json.dumps({'setup_diagnostics':{k:v for k,v in CHECKS.items() if k.startswith('setup_')}}),flush=True)
            body=await page.locator('body').text_content()
            CHECKS['setup_error_categories']=[x for x in ['form verification','Setup changed','could not complete','format','Administrator identity'] if x in (body or '')]
            CHECKS['stage_setup_clicked']=True
            assert saved_response.status==303
            await page.wait_for_url(ORIGIN+'/admin/connections?saved=meta')
            text=await page.content();assert 'Secret configured' in text and synthetic_secret not in text
            CHECKS['encrypted_meta_setup_and_redaction']=True
            # Prove data and session persistence across only this disposable app's recreation.
            command(COMPOSE+['up','-d','--no-deps','--force-recreate','socialhub'])
            for _ in range(60):
                try:
                    r=await http.get(ORIGIN+'/readyz')
                    if r.status_code==200:break
                except httpx.HTTPError:pass
                await asyncio.sleep(.5)
            await page.reload();assert 'Secret configured' in await page.content();CHECKS['setup_and_session_survive_app_recreation']=True
            outsider=await browser.new_context(ignore_https_errors=True);await outsider.route('**/*',local_only);other=await outsider.new_page()
            await helpers.browser_finish(other,ORIGIN+'/admin/oidc/login','/admin/oidc/callback','outsider',credentials['password'])
            assert 'Administrator identity' in await other.content()
            assert not any(c['name']=='__Secure-socialhub-admin' for c in await outsider.cookies())
            CHECKS['real_nonadmin_denied']=True
            await page.get_by_role('button',name='Sign out',exact=True).click();await page.wait_for_load_state('networkidle')
            response=await page.goto(ORIGIN+'/admin/');assert response.status==403;CHECKS['logout_revokes_session']=True
            await browser.close()
    CHECKS.update(live_writes=False,production_changes=False,meta_requests=0)
    p=ROOT/'data/admin-lab-evidence.json';p.parent.mkdir(exist_ok=True);p.write_text(json.dumps(CHECKS,indent=2)+'\n')
    print(json.dumps(CHECKS,indent=2))
if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as e:
        import traceback
        frames=[f for f in traceback.extract_tb(e.__traceback__) if Path(f.filename).name==Path(__file__).name]
        print(json.dumps({'admin_lab':'failed','error_type':type(e).__name__,'line':frames[-1].lineno if frames else None,'completed_checks':list(CHECKS),'safe_setup_diagnostics':{k:v for k,v in CHECKS.items() if k.startswith('setup_')}}));raise SystemExit(1)
