"""Resource server boundaries with synthetic signatures; never calls Meta."""
import time
from types import SimpleNamespace
import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from app.config import Settings
from app.main import create_server
from app.security.auth import Verifier
from app.services.meta_oauth import MetaOAuth
from app.services.approval_portal import OwnerPortal

ORIGIN='https://socialhub.example.test'

def settings(**changes):
 return Settings(_env_file=None,**({'database_url':'sqlite://','encryption_key':Fernet.generate_key().decode(),
  'local_token':'a'*64,'public_url':ORIGIN+'/mcp','browser_origin':ORIGIN,
  'meta_redirect_uri':ORIGIN+'/meta/oauth/callback','oauth_audience':ORIGIN+'/mcp',
  'oauth_issuer':'https://auth.example.test','oauth_jwks_url':'https://auth.example.test/jwks.json',
  'oauth_allowed_subjects':['owner-sub'],'oauth_allowed_client_ids':['mcp-client']}|changes))

@pytest.mark.parametrize('change',[
 {'browser_origin':'https://evil.test/path'},{'browser_origin':'https://socialhub.example.test/'},
 {'meta_redirect_uri':ORIGIN+'/wrong'},{'public_url':ORIGIN+'/socialhub/mcp'},
 {'public_url':'https://user:password@evil.test/mcp'},{'public_url':'http://external.test/mcp'},
 {'browser_origin':'https://*.example.test'},{'browser_origin':'https://evil.test?next=1'},
 {'trusted_proxy_cidrs':['not-a-network']},{'auth_mode':'oauth','oauth_audience':'https://wrong.test/mcp'}])
def test_configuration_rejects_ambiguous_or_untrusted_origins(change):
 with pytest.raises(ValueError):settings(**change)

@pytest.mark.parametrize('change,header',[
 ({},'at+jwt'),({'sub':'outsider'},'at+jwt'),({'client_id':'portal-client'},'at+jwt'),
 ({'scope':''},'at+jwt'),({'scope':'socialhub:publish'},'at+jwt'),
 ({'aud':'wrong'},'at+jwt'),({'iss':'https://wrong.test'},'at+jwt'),
 ({'exp':1},'at+jwt'),({'nbf':int(time.time())+1000},'at+jwt'),({},'JWT'),
 ({'scope':['socialhub:read']},'at+jwt'),({'azp':'other'},'at+jwt')])
async def test_jwt_identity_type_and_resource_boundaries(monkeypatch,change,header):
 s=settings(auth_mode='oauth');v=Verifier(s);key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 monkeypatch.setattr(v.jwks,'get_signing_key_from_jwt',lambda _:SimpleNamespace(key=key.public_key()))
 claims={'sub':'owner-sub','client_id':'mcp-client','scope':'socialhub:read','iss':s.oauth_issuer,
  'aud':s.public_url,'iat':int(time.time()),'exp':int(time.time())+60}|change
 result=await v.verify_token(jwt.encode(claims,key,algorithm='RS256',headers={'typ':header}))
 assert bool(result)==(not change and header=='at+jwt')
 assert await v.verify_token('malformed') is None

async def test_canonical_metadata_challenge_and_all_route_host_origin_boundary():
 app=create_server(settings(auth_mode='oauth',media_delivery_enabled=False)).streamable_http_app()
 async with app.router.lifespan_context(app):
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as c:
   r=await c.post('/mcp',json={});assert r.status_code==401
   assert ORIGIN+'/.well-known/oauth-protected-resource/mcp' in r.headers['www-authenticate']
   d=(await c.get('/.well-known/oauth-protected-resource/mcp')).json()
   assert d['resource']==ORIGIN+'/mcp' and d['authorization_servers']==['https://auth.example.test']
   cors=await c.options('/.well-known/oauth-protected-resource/mcp',headers={'Origin':ORIGIN,'Access-Control-Request-Method':'GET'})
   assert cors.status_code==200 and cors.headers['access-control-allow-origin']=='*'
   for path in ['/mcp','/.well-known/oauth-protected-resource/mcp','/approval/','/meta/oauth/start','/media/example']:
    assert (await c.get(path,headers={'Host':'evil.test','X-Forwarded-Host':'socialhub.example.test'})).status_code==421
    assert (await c.get(path,headers={'Origin':'https://evil.test'})).status_code==403
   assert (await c.get('/media/example')).status_code==404

async def test_meta_and_admin_portal_links_use_configured_origin(sessions):
 s=settings(meta_app_id='12345',meta_app_secret='synthetic',approval_portal_enabled=True)
 assert MetaOAuth(s,sessions).issue_login().startswith(ORIGIN+'/meta/oauth/start?')
 assert OwnerPortal(s,sessions).issue_login('owner').startswith(ORIGIN+'/approval/login?')

async def test_forwarded_proto_requires_trusted_peer_and_cannot_spoof_host():
 s=settings(approval_portal_enabled=True)
 app=create_server(s).streamable_http_app()
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('192.0.2.10',123)),base_url='http://socialhub.example.test') as c:
  assert (await c.get('/approval/login?ticket=synthetic',headers={'X-Forwarded-Proto':'https'})).status_code==403

@pytest.mark.parametrize('scope,scp,valid',[(None,['socialhub:read'],True),
 ('socialhub:read',['socialhub:read'],True),('socialhub:publish',['socialhub:read'],False),
 (None,'socialhub:read',False),(None,[123],False),(None,['socialhub:read socialhub:publish'],False),
 (None,[],False)])
async def test_authelia_scp_normalization_is_strict(monkeypatch,scope,scp,valid):
 s=settings(auth_mode='oauth');v=Verifier(s);key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 monkeypatch.setattr(v.jwks,'get_signing_key_from_jwt',lambda _:SimpleNamespace(key=key.public_key()))
 claims={'sub':'owner-sub','client_id':'mcp-client','iss':s.oauth_issuer,'aud':s.public_url,
  'iat':int(time.time()),'exp':int(time.time())+60,'scp':scp}
 if scope is not None:claims['scope']=scope
 assert bool(await v.verify_token(jwt.encode(claims,key,algorithm='RS256',headers={'typ':'at+jwt'})))==valid

async def test_existing_local_tls_proxy_boundary_keeps_native_callback():
 s=Settings(_env_file=None,database_url='sqlite://',encryption_key=Fernet.generate_key().decode(),
  local_token='a'*64,trusted_proxy_cidrs=['172.21.0.3/32'])
 app=create_server(s).streamable_http_app()
 for peer in ['172.21.0.3','192.0.2.10']:
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=(peer,123)),base_url='http://localhost:8788') as c:
   # No configured Meta credentials and no HTTP call to Meta; origin must pass first for trusted peer.
   r=await c.get('/meta/oauth/start?ticket=invalid',headers={'X-Forwarded-Proto':'https'})
   assert r.status_code==(400 if peer=='172.21.0.3' else 403)
