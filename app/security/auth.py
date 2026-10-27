import asyncio
import secrets
import time
import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier


class VerifiedAccessToken(AccessToken):
    """Server-derived authentication context; never populated from tool arguments."""
    oauth_client_id: str | None = None
    authentication_method: str = 'local'


class Verifier(TokenVerifier):
    def __init__(self, settings):
        self.settings = settings
        self.jwks = jwt.PyJWKClient(settings.oauth_jwks_url, cache_jwk_set=True, lifespan=300, timeout=5) if settings.auth_mode == 'oauth' else None

    async def verify_token(self, token):
        s = self.settings
        if s.auth_mode == 'local':
            if not secrets.compare_digest(token, s.local_token.get_secret_value()):
                return None
            return VerifiedAccessToken(token=token, client_id='local-developer', scopes=['socialhub:read', 'socialhub:mock-write', 'socialhub:media', 'socialhub:publish'], expires_at=int(time.time()) + 3600, resource=s.public_url)
        try:
            if jwt.get_unverified_header(token).get('typ') != 'at+jwt':
                return None
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(token, key.key, algorithms=['RS256'], audience=s.oauth_audience,
                                issuer=s.oauth_issuer, options={'require': ['exp', 'iat', 'sub', 'aud', 'iss']})
            scope = claims.get('scope')
            scp = claims.get('scp')
            # Authelia's signed access-token profile uses an scp string array.
            # Normalize only this explicit representation; reject ambiguity.
            if scope is not None and not isinstance(scope, str):
                return None
            if scp is not None and (not isinstance(scp, list) or len(scp) > 64 or any(
                    not isinstance(v, str) or not v or len(v) > 128 or any(c.isspace() for c in v) for v in scp)):
                return None
            if scope is not None and scp is not None and set(scope.split()) != set(scp):
                return None
            scopes = scope.split() if scope is not None else scp or []
            client = claims.get('azp', claims.get('client_id'))
            if client is not None and (not isinstance(client, str) or not client or len(client) > 128):
                return None
            if claims.get('azp') is not None and claims.get('client_id') is not None and claims['azp'] != claims['client_id']:
                return None
            if not isinstance(claims['sub'], str) or not 1 <= len(claims['sub']) <= 128:
                return None
            if (claims['sub'] not in s.oauth_allowed_subjects or client not in s.oauth_allowed_client_ids
                    or 'socialhub:read' not in scopes):
                return None
            return VerifiedAccessToken(token=token, client_id=claims['sub'], scopes=scopes, expires_at=claims['exp'], resource=s.public_url,
                                       oauth_client_id=client, authentication_method='oauth')
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return None
