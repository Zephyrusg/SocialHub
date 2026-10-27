"""Independent OIDC owner authentication; synthetic provider and keys only."""
import time
from types import SimpleNamespace
from urllib.parse import parse_qs,urlsplit
from datetime import timedelta
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from starlette.datastructures import QueryParams
from app.core import HubError
from app.database.models import OwnerOIDCFlow, ApprovalPortalSession, utcnow
from app.services.owner_oidc import OwnerOIDC
from test_production_auth import settings

@pytest.fixture
async def oidc(sessions,monkeypatch):
 s=settings(approval_portal_enabled=True,owner_oidc_enabled=True,owner_oidc_client_id='portal-client',
  owner_oidc_client_secret='synthetic-client-secret',owner_oidc_allowed_subjects=['owner-sub'])
 key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 state={'change':{},'typ':'JWT','requests':[]}
 def transport(request):
  if request.url.path.endswith('openid-configuration'):
   return httpx.Response(200,json={'issuer':s.oauth_issuer,'jwks_uri':s.oauth_jwks_url,
    'authorization_endpoint':s.oauth_issuer+'/authorize','token_endpoint':s.oauth_issuer+'/token',
    'code_challenge_methods_supported':['S256'],'token_endpoint_auth_methods_supported':['client_secret_basic'],
    'authorization_response_iss_parameter_supported':True}|state.get('metadata',{}))
  state['requests'].append(request)
  claims={'iss':s.oauth_issuer,'aud':'portal-client','sub':'owner-sub','iat':int(time.time()),'exp':int(time.time())+60,
   'nonce':state['nonce'],'amr':['pwd','otp']}|state['change']
  return httpx.Response(200,json={'id_token':jwt.encode(claims,key,algorithm='RS256',headers={'typ':state['typ']})})
 service=OwnerOIDC(s,sessions,httpx.MockTransport(transport))
 monkeypatch.setattr(service.keys,'get_signing_key_from_jwt',lambda _:SimpleNamespace(key=key.public_key()))
 url,browser=await service.start();query=parse_qs(urlsplit(url).query)
 state['nonce']=query['nonce'][0]
 return service,state,query,browser

def params(service,query,**change):
 return QueryParams({'state':query['state'][0],'code':'synthetic-one-use-code','iss':service.settings.oauth_issuer}|change)

async def test_real_session_model_state_pkce_nonce_hashes_and_replay(oidc,sessions):
 service,state,query,browser=oidc
 assert query['code_challenge_method']==['S256'] and query['scope']==['openid']
 with sessions() as db:
  flow=db.scalar(select(OwnerOIDCFlow));assert flow.state_hash!=query['state'][0]
  assert browser not in flow.browser_hash and state['nonce'] not in flow.ciphertext
 token=await service.complete(params(service,query),browser)
 assert service.portal.authenticate(token)=='owner-sub'
 request=state['requests'][0];fields=parse_qs(request.content.decode())
 assert 'code_verifier' in fields and request.headers['authorization'].startswith('Basic ')
 with pytest.raises(HubError):await service.complete(params(service,query),browser)
 service.portal.logout(token)
 with pytest.raises(HubError):service.portal.authenticate(token)

@pytest.mark.parametrize('change',[
 {'sub':'outsider'},{'nonce':'forged'},{'iss':'https://wrong.test'},
 {'aud':'mcp-client'},{'exp':1},{'azp':'mcp-client'},{'amr':['pwd']},
 {'aud':['portal-client','other']},{'nonce':None}])
async def test_bad_identity_claims_fail_closed(oidc,sessions,change):
 service,state,query,browser=oidc;state['change']=change
 with pytest.raises((HubError,jwt.PyJWTError,TypeError)):
  await service.complete(params(service,query),browser)
 with sessions() as db:assert db.scalar(select(ApprovalPortalSession)) is None

@pytest.mark.parametrize('change',[
 {'state':'forged'},{'iss':'https://wrong.test'},{'code':''},{'error':'access_denied'}])
async def test_invalid_callback_no_owner_session(oidc,sessions,change):
 service,state,query,browser=oidc
 with pytest.raises(HubError):await service.complete(params(service,query,**change),browser)
 with sessions() as db:assert db.scalar(select(ApprovalPortalSession)) is None

async def test_access_token_cannot_log_into_portal_and_cookie_binding(oidc):
 service,state,query,browser=oidc
 with pytest.raises(HubError):await service.complete(params(service,query),'forged-cookie')
 state['typ']='at+jwt'
 with pytest.raises(HubError):await service.complete(params(service,query),browser)

async def test_expired_flow_and_provider_metadata(oidc,sessions):
 service,state,query,browser=oidc
 with sessions.begin() as db:db.scalar(select(OwnerOIDCFlow)).expires_at=utcnow()-timedelta(seconds=1)
 with pytest.raises(HubError):await service.complete(params(service,query),browser)
 for metadata in [{'issuer':'https://evil.test'},{'jwks_uri':'https://evil.test/keys'},
                  {'code_challenge_methods_supported':['plain']},{'token_endpoint':'https://evil.test/token'}]:
  state['metadata']=metadata
  with pytest.raises(ValueError):await service.start()
