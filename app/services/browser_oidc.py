"""Separate confidential OIDC client; no MCP or Meta credentials accepted here."""
import asyncio
import base64
from datetime import timedelta
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode, urlsplit
import httpx
import jwt
from sqlalchemy import delete, select, update
from app.config import validate_url
from app.core import HubError
from app.database.models import OwnerOIDCFlow, utcnow
from app.security.tokens import TokenVault
from app.services.approval_portal import token_hash


class BrowserOIDC:
    """Shared verified browser login; each consumer has separate cookies/sessions/purpose."""
    def __init__(self, settings, sessions, *, client_id, client_secret, allowed_subjects,
                 callback_path, purpose, session_backend, enabled, require_mfa=True,
                 max_age=None, transport=None):
        self.settings, self.sessions, self.transport = settings, sessions, transport
        self.client_id, self.client_secret = client_id, client_secret
        self.allowed_subjects, self.callback_path = allowed_subjects, callback_path
        self.purpose, self.portal, self.active = purpose, session_backend, enabled
        self.require_mfa, self.max_age = require_mfa, max_age
        self.vault = TokenVault([settings.encryption_key.get_secret_value()])
        self.keys = jwt.PyJWKClient(settings.oauth_jwks_url, lifespan=300, timeout=5) if enabled else None

    def enabled(self):
        self.portal.enabled()
        if not self.active:
            raise HubError('portal_denied', 'Browser OIDC is not configured')

    async def metadata(self, client):
        s = self.settings
        response = await client.get(s.oauth_issuer.rstrip('/') + '/.well-known/openid-configuration')
        response.raise_for_status()
        data = response.json()
        if (data.get('issuer') != s.oauth_issuer or data.get('jwks_uri') != s.oauth_jwks_url
                or 'S256' not in data.get('code_challenge_methods_supported', [])
                or 'client_secret_basic' not in data.get('token_endpoint_auth_methods_supported', [])):
            raise ValueError('Provider metadata does not match the configured OIDC contract')
        for key in ('authorization_endpoint', 'token_endpoint'):
            validate_url(data[key])
            if urlsplit(data[key]).netloc != urlsplit(s.oauth_issuer).netloc:
                raise ValueError('OIDC endpoints must use the trusted issuer authority')
        return data

    async def start(self):
        self.enabled()
        state, browser, nonce, verifier = [secrets.token_urlsafe(32) for _ in range(4)]
        async with httpx.AsyncClient(transport=self.transport, timeout=10, follow_redirects=False) as client:
            metadata = await self.metadata(client)
        with self.sessions.begin() as db:
            db.execute(delete(OwnerOIDCFlow).where(OwnerOIDCFlow.expires_at <= utcnow()))
            db.add(OwnerOIDCFlow(state_hash=token_hash(state), browser_hash=token_hash(browser),
                ciphertext=self.vault.encrypt({'nonce': nonce, 'verifier': verifier,
                    'issuer_response': metadata.get('authorization_response_iss_parameter_supported', False),
                    'purpose': self.purpose, 'client_id': self.client_id, 'callback_path': self.callback_path}),
                expires_at=utcnow() + timedelta(minutes=5)))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        query = {'client_id': self.client_id, 'response_type': 'code',
            'redirect_uri': self.settings.browser_origin + self.callback_path,
            'scope': 'openid', 'state': state, 'nonce': nonce, 'code_challenge': challenge,
            'code_challenge_method': 'S256'}
        if self.max_age is not None:
            query['max_age'] = str(self.max_age)
        return metadata['authorization_endpoint'] + '?' + urlencode(query), browser

    async def complete(self, params, browser):
        self.enabled()
        if (any(len(params.getlist(k)) != 1 for k in ('state', 'code'))
                or len(params.getlist('iss')) > 1 or 'error' in params
                or not browser or len(browser) > 128 or len(params['state']) > 128
                or not params['code'] or len(params['code']) > 8192):
            raise HubError('portal_denied', 'Invalid OIDC callback')
        with self.sessions.begin() as db:
            flow = db.scalar(select(OwnerOIDCFlow).where(OwnerOIDCFlow.state_hash == token_hash(params['state']),
                OwnerOIDCFlow.browser_hash == token_hash(browser), OwnerOIDCFlow.expires_at > utcnow(),
                OwnerOIDCFlow.consumed.is_(False)))
            if flow is None:
                raise HubError('portal_denied', 'OIDC exchange expired or replayed')
            result = db.execute(update(OwnerOIDCFlow).where(OwnerOIDCFlow.state_hash == flow.state_hash,
                OwnerOIDCFlow.consumed.is_(False)).values(consumed=True))
            if result.rowcount != 1:
                raise HubError('portal_denied', 'OIDC exchange replayed')
            stored = json.loads(self.vault.cipher.decrypt(flow.ciphertext.encode()))
        s = self.settings
        if (stored.get('purpose') != self.purpose or stored.get('client_id') != self.client_id
                or stored.get('callback_path') != self.callback_path):
            raise HubError('portal_denied', 'OIDC flow belongs to another browser client')
        if ((stored['issuer_response'] and params.get('iss') != s.oauth_issuer)
                or ('iss' in params and params['iss'] != s.oauth_issuer)):
            raise HubError('portal_denied', 'OIDC response issuer mismatch')
        async with httpx.AsyncClient(transport=self.transport, timeout=10, follow_redirects=False) as client:
            metadata = await self.metadata(client)
            response = await client.post(metadata['token_endpoint'], auth=(self.client_id, self.client_secret.get_secret_value()),
                data={'grant_type': 'authorization_code', 'code': params['code'],
                    'redirect_uri': s.browser_origin + self.callback_path, 'code_verifier': stored['verifier']})
            response.raise_for_status()
            token = response.json()['id_token']
        if jwt.get_unverified_header(token).get('typ') != 'JWT':
            raise HubError('portal_denied', 'An OIDC ID token is required')
        key = await asyncio.to_thread(self.keys.get_signing_key_from_jwt, token)
        claims = jwt.decode(token, key.key, algorithms=['RS256'], audience=self.client_id,
            issuer=s.oauth_issuer, options={'require': ['sub', 'exp', 'iat', 'iss', 'aud', 'nonce']})
        if (not isinstance(claims['sub'], str) or claims['sub'] not in self.allowed_subjects
                or not isinstance(claims['nonce'], str) or not secrets.compare_digest(claims['nonce'], stored['nonce'])
                or claims.get('azp', self.client_id) != self.client_id
                or (isinstance(claims['aud'], list) and len(claims['aud']) > 1 and 'azp' not in claims)
                or (self.require_mfa and not {'pwd', 'otp'}.issubset(set(claims.get('amr', []))))):
            raise HubError('portal_denied', 'Independent owner identity or MFA verification failed')
        if self.max_age is not None:
            auth_time = claims.get('auth_time')
            if (not isinstance(auth_time, (int, float)) or isinstance(auth_time, bool)
                    or not time.time() - self.max_age <= auth_time <= time.time() + 5):
                raise HubError('portal_denied', 'Recent authenticated owner login required')
        return await asyncio.to_thread(self.portal.create_session, claims['sub'])
