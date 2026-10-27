"""Real synthetic Authelia/browser -> JWT -> MCP + independent portal tests.
No screenshots/traces, credential-bearing output, production hosts or Meta requests.
Run using the separate .tools/oidc-venv; see docs/development.md.
"""
import asyncio
import base64
import hashlib
import hmac
import struct
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import httpx
import jwt
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
SECRETS=ROOT/'secrets/local-oidc'
ISSUER='https://auth.socialhub.test:9443'
BROWSER='https://hub.socialhub.test:9444'
RESOURCE='https://socialhub.example.test/mcp'
COMPOSE=['docker','compose','--project-name','socialhub-oidc-test','-f',str(ROOT/'deployment/local-oidc/compose.yml')]
PROGRESS={}
TOTP_LAST_STEP={}
_original=socket.getaddrinfo

def local_resolve(host,*args,**kwargs):
 name=host.decode() if isinstance(host,bytes) else host
 if name in ('auth.socialhub.test','hub.socialhub.test'):host='127.0.0.1'
 return _original(host,*args,**kwargs)
socket.getaddrinfo=local_resolve


def bind(subjects):
 path=SECRETS/'socialhub.env';text=path.read_text()
 for key,value in subjects.items():
  text='\n'.join(key+'='+json.dumps(value) if line.startswith(key+'=') else line for line in text.splitlines())+'\n'
 path.write_text(text)
 result=subprocess.run(COMPOSE+['up','-d','--no-deps','socialhub'],capture_output=True)
 assert result.returncode==0,'Local test app restart failed'


def rotate_signer():
 path=SECRETS/'configuration.yml';config=yaml.safe_load(path.read_text())
 key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 kid='local-rotation-'+secrets.token_hex(4)
 oidc=config['identity_providers']['oidc']
 oidc['jwks'].insert(0,{'key_id':kid,'algorithm':'RS256','use':'sig',
  'key':key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()})
 oidc['clients'][0]['access_token_signed_response_key_id']=kid
 path.write_text(yaml.safe_dump(config,sort_keys=False))
 result=subprocess.run(COMPOSE+['restart','authelia'],capture_output=True)
 assert result.returncode==0,'Isolated signing rotation restart failed'
 return kid


async def enter_totp(page,username):
 seeds=json.loads((SECRETS/'test-credentials.json').read_text())['totp']
 step=int(time.time()//30)
 if TOTP_LAST_STEP.get(username)==step:
  await asyncio.sleep(30-time.time()%30+0.1)
  step=int(time.time()//30)
 seed=seeds[username];key=base64.b32decode(seed+'='*((-len(seed))%8))
 digest=hmac.new(key,struct.pack('>Q',step),hashlib.sha1).digest();offset=digest[-1]&15
 code=str((struct.unpack('>I',digest[offset:offset+4])[0]&0x7fffffff)%1000000).zfill(6)
 field=page.locator('input[autocomplete="one-time-code"]:visible').first
 await field.focus();await field.press_sequentially(code,delay=50)
 TOTP_LAST_STEP[username]=step
 await page.wait_for_timeout(1000)


async def browser_finish(page, start, callback_path, username, password):
 await page.goto(start)
 await page.wait_for_load_state('networkidle')
 await page.wait_for_timeout(500)
 for _ in range(160):
  if urlsplit(page.url).path==callback_path:return parse_qs(urlsplit(page.url).query)
  user=page.locator('#username-textfield')
  if await user.is_visible():
   await user.clear();await user.press_sequentially(username,delay=25)
   await page.locator('#password-textfield').clear();await page.locator('#password-textfield').press_sequentially(password,delay=10)
   await page.get_by_role('button',name='Sign in',exact=True).click()
   await page.wait_for_timeout(1200)
  elif await page.locator('input[autocomplete="one-time-code"]:visible').count():
   await enter_totp(page,username)
  else:
   for label in ('Accept','Allow','Consent','Authorize'):
    button=page.get_by_role('button',name=label,exact=True)
    if await button.count() and await button.is_visible():
     await button.click();break
  await page.wait_for_timeout(250)
 raise AssertionError('Local browser did not finish login/consent; inspect provider health and synthetic client configuration')


async def grant(page, client, metadata, credentials, *, owner=False, resource=RESOURCE, token_resource=RESOURCE, scope='openid socialhub:read'):
 verifier=secrets.token_urlsafe(48);state=secrets.token_urlsafe(32);nonce=secrets.token_urlsafe(32)
 challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
 client_id='socialhub-owner-local' if owner else 'socialhub-chatgpt-local'
 callback=BROWSER+('/approval/oidc/callback' if owner else '/test/callback')
 async def capture(route):await route.fulfill(status=200,body='Local OAuth response captured without logging.')
 await page.route(callback+'*',capture)
 query={'client_id':client_id,'redirect_uri':callback,'response_type':'code','scope':'openid' if owner else scope,
  'state':state,'nonce':nonce,'code_challenge':challenge,'code_challenge_method':'S256'}
 if resource is not None and not owner:query['resource']=resource
 result=await browser_finish(page,metadata['authorization_endpoint']+'?'+urlencode(query),urlsplit(callback).path,'owner',credentials['password'])
 await page.unroute(callback+'*',capture)
 assert result.get('state')==[state]
 assert result.get('iss')==[metadata['issuer']], 'Authorization response issuer absent/mismatched'
 if 'error' in result:
  print(json.dumps({'oauth_stage':'authorization_response','error':result['error'][0],'description_categories':[word for word in ('resource','audience','scope','client','PKCE','redirect','unsupported') if word.lower() in result.get('error_description',[''])[0].lower()]}))
  return {'error':result['error'][0]},None,None
 data={'grant_type':'authorization_code','client_id':client_id,'code':result['code'][0],
  'redirect_uri':callback,'code_verifier':verifier}
 if token_resource is not None and not owner:data['resource']=token_resource
 response=await client.post(metadata['token_endpoint'],data=data,
  auth=(client_id,credentials['portal_secret']) if owner else None)
 body=response.json()
 if 'error' in body:
  print(json.dumps({'oauth_stage':'token_exchange','status':response.status_code,'error':body['error'],'description_categories':[word for word in ('resource','audience','scope','client','PKCE','redirect','unsupported','code') if word.lower() in body.get('error_description','').lower()]}))
 return body,data,result


def rpc(method,params=None):
 return {'jsonrpc':'2.0','id':1,'method':method,**({'params':params} if params is not None else {})}


def unpack(response):
 if response.status_code!=200:print(json.dumps({'mcp_http_status':response.status_code}))
 assert response.status_code==200,'MCP request rejected'
 return json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))


async def main():
 evidence=PROGRESS;credentials=json.loads((SECRETS/'test-credentials.json').read_text())
 ctx=ssl.create_default_context(cafile=str(SECRETS/'ca.crt'))
 async with httpx.AsyncClient(verify=ctx,trust_env=False,timeout=15,follow_redirects=False) as client:
  metadata=None
  for _ in range(80):
   try:
    response=await client.get(ISSUER+'/.well-known/openid-configuration')
    if response.status_code==200:
     metadata=response.json();break
   except (httpx.HTTPError,ValueError):pass
   await asyncio.sleep(0.25)
  assert metadata is not None,'Local provider discovery startup timed out'
  assert metadata['issuer']==ISSUER
  assert 'S256' in metadata['code_challenge_methods_supported']
  assert {'none','client_secret_basic'} <= set(metadata['token_endpoint_auth_methods_supported'])
  keys=(await client.get(metadata['jwks_uri'])).json()
  evidence['discovery']={k:metadata[k] for k in ['issuer','jwks_uri','authorization_response_iss_parameter_supported','code_challenge_methods_supported','token_endpoint_auth_methods_supported']}
  evidence['jwks_key_count']=len(keys['keys'])
  def verify(token,audience,typ):
   header=jwt.get_unverified_header(token);assert header['typ']==typ
   key=next(k for k in keys['keys'] if k['kid']==header['kid'])
   return jwt.decode(token,jwt.PyJWK.from_dict(key).key,algorithms=['RS256'],issuer=ISSUER,audience=audience)
  async with async_playwright() as pw:
   browser=await pw.chromium.launch(args=['--host-resolver-rules=MAP auth.socialhub.test 127.0.0.1, MAP hub.socialhub.test 127.0.0.1','--no-proxy-server'])
   context=await browser.new_context(ignore_https_errors=True) # disposable test CA only; Python/resource server uses CA validation
   page=await context.new_page()
   tokens,data,_=await grant(page,client,metadata,credentials)
   assert 'access_token' in tokens,'Authorization-code/token exchange failed'
   claims=verify(tokens['access_token'],RESOURCE,'at+jwt')
   print(json.dumps({'issued_access_token_claim_names':sorted(claims),'scope_type':type(claims.get('scope')).__name__,'client_id_present':'client_id' in claims,'azp_present':'azp' in claims}))
   assert 'socialhub:read' in claims.get('scp',[]) and claims.get('client_id',claims.get('azp'))=='socialhub-chatgpt-local'
   evidence['code_pkce_public_none']=True;evidence['resource_authorize_and_token']=True
   evidence['access_token']={'typ':'at+jwt','alg':'RS256','aud':claims['aud'],'scp':claims['scp'],'lifetime_seconds':claims['exp']-claims['iat']}
   replay=await client.post(metadata['token_endpoint'],data=data)
   assert replay.status_code==400 and replay.json().get('error')=='invalid_grant'
   evidence['authorization_code_replay_rejected']=True
   owner_tokens,_,_=await grant(page,client,metadata,credentials,owner=True)
   owner_claims=verify(owner_tokens['id_token'],'socialhub-owner-local','JWT')
   assert {'pwd','otp'}.issubset(set(owner_claims.get('amr',[])))
   evidence['real_owner_mfa']=True
   bind({'OAUTH_ALLOWED_SUBJECTS':[claims['sub']],'OWNER_OIDC_ALLOWED_SUBJECTS':[owner_claims['sub']]})
   for _ in range(80):
    try:
     ready=await client.post(BROWSER+'/mcp',json={},headers={'Host':'socialhub.example.test'})
     if ready.status_code==401:break
    except httpx.HTTPError:pass
    await asyncio.sleep(0.25)
   # New grant after a service recreate avoids relying on remaining token lifetime.
   tokens,_,_=await grant(page,client,metadata,credentials)
   headers={'Host':'socialhub.example.test','Accept':'application/json, text/event-stream'}
   challenge=await client.post(BROWSER+'/mcp',json={},headers=headers)
   assert challenge.status_code==401 and RESOURCE.replace('/mcp','/.well-known/oauth-protected-resource/mcp') in challenge.headers['www-authenticate']
   resource_metadata=(await client.get(BROWSER+'/.well-known/oauth-protected-resource/mcp',headers=headers)).json()
   assert resource_metadata['resource']==RESOURCE
   headers['Authorization']='Bearer '+tokens['access_token']
   init=unpack(await client.post(BROWSER+'/mcp',headers=headers,json=rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'local-oidc-test','version':'1'}})))
   assert init['result']['serverInfo']['name']=='SocialHub'
   tools=unpack(await client.post(BROWSER+'/mcp',headers=headers,json=rpc('tools/list')))['result']['tools']
   assert all('securitySchemes' in t.get('_meta',{}) for t in tools)
   result=unpack(await client.post(BROWSER+'/mcp',headers=headers,json=rpc('tools/call',{'name':'list_accounts','arguments':{}})))
   assert result['result']['structuredContent']['ok'] is True
   posts=unpack(await client.post(BROWSER+'/mcp',headers=headers,json=rpc('tools/call',{'name':'list_posts','arguments':{'account_id':'mock-facebook'}})))
   assert posts['result']['structuredContent']['ok'] is True
   evidence['real_jwt_mcp_read']=True;evidence['mock_posts_read']=True;evidence['tool_count']=len(tools)
   denied=unpack(await client.post(BROWSER+'/mcp',headers=headers,json=rpc('tools/call',{'name':'prepare_publication','arguments':{'account_ids':['pixelpapa-facebook'],'text':'Synthetic, never dispatched','idempotency_key':'oidc-negative'}})))
   assert denied['result']['structuredContent']['error']['code']=='insufficient_scope'
   evidence['read_token_cannot_prepare_write']=True
   headers['Authorization']='Bearer '+owner_tokens['id_token']
   assert (await client.post(BROWSER+'/mcp',headers=headers,json={})).status_code==401
   evidence['id_token_rejected_by_resource_server']=True
   for name,kwargs in [('wrong_authorize_resource',{'resource':'https://wrong.example.test/mcp'}),
                       ('wrong_token_resource',{'token_resource':'https://wrong.example.test/mcp'})]:
    wrong,_,_=await grant(page,client,metadata,credentials,**kwargs)
    assert 'access_token' not in wrong
    evidence[name]=wrong.get('error','rejected')
   no_scope,_,_=await grant(page,client,metadata,credentials,scope='openid')
   headers['Authorization']='Bearer '+no_scope['access_token']
   assert (await client.post(BROWSER+'/mcp',headers=headers,json={})).status_code==401
   evidence['real_missing_scope_rejected']=True
   # Real service consumes and validates the separate portal flow, rather than the harness.
   await browser_finish(page,BROWSER+'/approval/oidc/login','/approval/','owner',credentials['password'])
   assert 'SocialHub owner approvals' in await page.title()
   assert await page.get_by_role('button',name='Sign out').count()==1
   portal_cookie=next(c for c in await context.cookies() if c['name']=='__Secure-socialhub-owner')
   assert portal_cookie['secure'] and portal_cookie['httpOnly'] and portal_cookie['sameSite']=='Lax'
   portal_headers={'Origin':BROWSER,'Cookie':portal_cookie['name']+'='+portal_cookie['value']}
   forged=await client.post(BROWSER+'/approval/logout',data={'csrf':'x'*64},headers=portal_headers)
   assert forged.status_code==403
   async with page.expect_navigation():
    await page.get_by_role('button',name='Sign out').click()
   if 'Signed out' not in await page.content():
    print(json.dumps({'portal_logout_page_denied':'portal_denied' in await page.content()}))
   assert 'Signed out' in await page.content()
   assert (await client.get(BROWSER+'/approval/',headers=portal_headers)).status_code==403
   evidence['portal_owner_login_csrf_logout']=True
   other=await browser.new_context(ignore_https_errors=True);other_page=await other.new_page()
   await browser_finish(other_page,BROWSER+'/approval/oidc/login','/approval/oidc/callback','outsider',credentials['password'])
   assert 'portal_denied' in await other_page.content()
   evidence['portal_nonowner_denied']=True
   outsider_tokens,_,_=await grant(other_page,client,metadata,credentials)
   headers['Authorization']='Bearer '+outsider_tokens['access_token']
   assert (await client.post(BROWSER+'/mcp',headers=headers,json={})).status_code==401
   evidence['real_nonowner_mcp_denied']=True
   new_kid=rotate_signer()
   for _ in range(80):
    try:
     keys=(await client.get(metadata['jwks_uri'])).json()
     if any(k['kid']==new_kid for k in keys['keys']):break
    except (httpx.HTTPError,ValueError):pass
    await asyncio.sleep(0.25)
   rotated,_,_=await grant(page,client,metadata,credentials)
   assert jwt.get_unverified_header(rotated['access_token'])['kid']==new_kid
   headers['Authorization']='Bearer '+rotated['access_token']
   rotation_start=time.monotonic()
   rotation_response=await client.post(BROWSER+'/mcp',headers=headers,json=rpc('tools/list'))
   evidence['rotation_initial_http_status']=rotation_response.status_code
   # PyJWT 2.15's 30-second unknown-kid cooldown prevents attacker-driven JWKS
   # fetch storms. Preserve it; key rotation must prepublish/overlap keys.
   for _ in range(45):
    if rotation_response.status_code==200:break
    assert rotation_response.status_code==401
    await asyncio.sleep(1)
    rotation_response=await client.post(BROWSER+'/mcp',headers=headers,json=rpc('tools/list'))
   assert rotation_response.status_code==200
   evidence['rotation_recovery_seconds']=round(time.monotonic()-rotation_start,2)
   evidence['real_signing_key_rotation_unknown_kid_refresh']=True
   # Actual issued token expiry; no synthetic manipulation.
   exp=jwt.decode(tokens['access_token'],options={'verify_signature':False})['exp']
   while time.time()<=exp:await asyncio.sleep(min(2,exp-time.time()+0.05))
   headers['Authorization']='Bearer '+tokens['access_token']
   assert (await client.post(BROWSER+'/mcp',headers=headers,json={})).status_code==401
   evidence['real_expired_token_rejected']=True
   await browser.close()
 evidence['mfa']='Real synthetic password + enrolled TOTP passed; both clients two_factor; portal requires validated pwd+otp.'
 path=ROOT/'data/local-oidc-evidence.json';path.parent.mkdir(exist_ok=True);path.write_text(json.dumps(evidence,indent=2)+'\n')
 print(json.dumps(evidence,indent=2))

if __name__=='__main__':
 try:asyncio.run(main())
 except Exception as error:
  # Playwright/httpx errors may contain complete OAuth URLs; never print exception text/locals.
  import traceback
  frames=[f for f in traceback.extract_tb(error.__traceback__) if Path(f.filename).name==Path(__file__).name]
  frame=frames[-1]
  print(json.dumps({'local_oidc_test':'FAILED','error_type':type(error).__name__,'location':f'{Path(frame.filename).name}:{frame.lineno}','completed_checks':list(PROGRESS)}))
  raise SystemExit(1)
