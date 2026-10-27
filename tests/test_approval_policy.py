"""All execute calls use FakeAdapter; no test can reach actual Meta writes."""
from datetime import timedelta
import time
from types import SimpleNamespace
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from app.config import Settings
from app.core import HubError
from app.database.models import PublicationApproval, Audit
from app.security.auth import VerifiedAccessToken, Verifier
from app.services.approval_policy import ApprovalPolicy
from test_publishing import publisher, prepare, approve


def native_settings(service, **changes):
    data = service.settings.model_dump() | {
        'auth_mode': 'oauth', 'oauth_issuer': 'https://issuer.example/',
        'oauth_jwks_url': 'https://issuer.example/keys', 'public_url': 'https://hub.example/mcp',
        'oauth_audience': 'https://hub.example/mcp', 'oauth_allowed_subjects': ['alice'], 'oauth_allowed_client_ids': ['chatgpt-client'], 'approval_mode': 'chatgpt_native',
        'chatgpt_confirmation_tested': True, 'chatgpt_confirmation_evidence_sha256': 'e' * 64,
        'chatgpt_residual_risk_accepted': True, 'chatgpt_allowed_client_ids': ['chatgpt-client'],
        'chatgpt_allowed_principals': ['alice']}
    return Settings.model_validate(data | changes)


def context(**changes):
    return VerifiedAccessToken(token='fake-not-live', client_id='alice', oauth_client_id='chatgpt-client',
        authentication_method='oauth', scopes=['socialhub:read', 'socialhub:publish'],
        expires_at=int(time.time()) + 3600).model_copy(update=changes)


def configure(service):
    service.settings = native_settings(service)
    service.approval_policy = ApprovalPolicy(service.settings)


async def test_default_owner_enforcement_unchanged(publisher):
    service, _, calls, _ = publisher
    p = await prepare(service)
    assert service.approval_requirements(p['publication_id'], 'alice', context())['independent_owner_required']
    with pytest.raises(HubError, match='exact approval'):
        await service.execute('alice', ['socialhub:publish'], p['publication_id'], context())
    assert not calls


@pytest.mark.parametrize('change', [
    {'auth_mode': 'local'}, {'chatgpt_confirmation_tested': False},
    {'chatgpt_confirmation_evidence_sha256': ''}, {'chatgpt_confirmation_evidence_sha256': 'z' * 64},
    {'chatgpt_residual_risk_accepted': False}, {'chatgpt_allowed_client_ids': []},
    {'chatgpt_allowed_principals': []}, {'chatgpt_allowed_operations': []}])
async def test_native_mode_missing_activation_gate_fails_startup(publisher, change):
    service, _, _, _ = publisher
    with pytest.raises(ValueError):
        native_settings(service, **change)


@pytest.mark.parametrize('ctx', [None, {'approved': True, 'oauth_client_id': 'chatgpt-client'},
    context(authentication_method='local'), context(oauth_client_id='impostor'), context(oauth_client_id=None),
    context(client_id='bob'), context(expires_at=1), context(scopes=['socialhub:read'])])
async def test_untrusted_or_wrong_context_cannot_mint_policy_grant(publisher, sessions, ctx):
    service, _, calls, _ = publisher
    configure(service)
    p = await prepare(service)
    with pytest.raises(HubError):
        await service.execute('alice', ['socialhub:publish'], p['publication_id'], ctx)
    with sessions() as db:
        assert db.scalar(select(PublicationApproval)) is None
    assert not calls


async def test_native_policy_reuses_shared_state_and_never_claims_independent_consent(publisher, sessions):
    service, _, calls, _ = publisher
    configure(service)
    p = await prepare(service)
    requirements = service.approval_requirements(p['publication_id'], 'alice', context())
    assert not requirements['independent_owner_required']
    assert not requirements['native_confirmation_receipt_verified']
    assert (await service.execute('alice', ['socialhub:publish'], p['publication_id'], context()))['state'] == 'in_progress'
    result = await service.execute('alice', ['socialhub:publish'], p['publication_id'], context())
    assert result['state'] == 'succeeded' and not result['independent_owner_approval']
    await service.execute('alice', ['socialhub:publish'], p['publication_id'], context())
    assert len(calls) == 2  # first synthetic delivery, then finalization; completed target never repeated
    with sessions() as db:
        grant = db.scalar(select(PublicationApproval))
        assert grant.channel == 'chatgpt_policy' and grant.oauth_client_id == 'chatgpt-client'
        assert grant.evidence_hash == service.approval_policy.fingerprint()
        audit = db.scalar(select(Audit).where(Audit.operation == 'authorize_publication_policy'))
        assert audit.details['independent_consent_verified'] is False


async def test_policy_revocation_change_and_expiry_never_renew_silently(publisher):
    service, _, calls, now = publisher
    configure(service)
    p = await prepare(service)
    await service.execute('alice', ['socialhub:publish'], p['publication_id'], context())
    service.settings.chatgpt_confirmation_evidence_sha256 = 'f' * 64
    with pytest.raises(HubError):
        await service.execute('alice', ['socialhub:publish'], p['publication_id'], context())
    service.settings.chatgpt_confirmation_evidence_sha256 = 'e' * 64
    now[0] += timedelta(hours=2)
    with pytest.raises(HubError):
        await service.execute('alice', ['socialhub:publish'], p['publication_id'], context())
    assert len(calls) == 1


async def test_owner_denial_prevents_later_policy_auto_authorization(publisher):
    service, _, calls, _ = publisher
    configure(service)
    p = await prepare(service)
    service.revoke(p['publication_id'], p['request_hash'], 'independent-owner')
    with pytest.raises(HubError):
        await service.execute('alice', ['socialhub:publish'], p['publication_id'], context())
    assert not calls


async def test_default_policy_requires_owner_for_edits_and_multi_target(publisher):
    service, _, _, _ = publisher
    configure(service)
    policy = service.approval_policy
    payload = {'request': {'operation': 'update', 'account_ids': ['pixelpapa-facebook']}}
    assert not policy.eligible('alice', payload, context())
    payload = {'request': {'operation': 'publish', 'account_ids': ['pixelpapa-facebook', 'pixelpapa-instagram']}}
    assert not policy.eligible('alice', payload, context())


async def test_client_identity_comes_from_verified_jwt_not_tool_metadata(publisher, monkeypatch):
    service, _, _, _ = publisher
    s = native_settings(service)
    verifier = Verifier(s)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(verifier.jwks, 'get_signing_key_from_jwt', lambda token: SimpleNamespace(key=key.public_key()))
    claims = {'iss': s.oauth_issuer, 'aud': s.oauth_audience, 'sub': 'alice', 'iat': int(time.time()),
              'exp': int(time.time()) + 60, 'scope': 'socialhub:read socialhub:publish', 'azp': 'chatgpt-client'}
    token = await verifier.verify_token(jwt.encode(claims, key, algorithm='RS256', headers={'typ': 'at+jwt'}))
    assert token.oauth_client_id == 'chatgpt-client' and token.client_id == 'alice'
    for changed in [{'azp': ['chatgpt-client']}, {'client_id': 'other-client'}, {'sub': 123}, {'azp': ''}]:
        assert await verifier.verify_token(jwt.encode(claims | changed, key, algorithm='RS256', headers={'typ': 'at+jwt'})) is None
    missing = dict(claims); del missing['azp']
    token = await verifier.verify_token(jwt.encode(missing, key, algorithm='RS256', headers={'typ': 'at+jwt'}))
    assert token is None  # an unbound OAuth client is refused
    assert not ApprovalPolicy(s).eligible('alice', {'request': {'operation': 'publish', 'account_ids': ['pixelpapa-facebook']}}, token)


async def test_requirements_distinguish_existing_grant_from_new_owner_interaction(publisher):
    service, _, _, _ = publisher
    p = await prepare(service)
    assert service.approval_requirements(p['publication_id'], 'alice')['needs_new_owner_grant']
    approve(service, p)
    result = service.approval_requirements(p['publication_id'], 'alice')
    assert result['owner_grant_valid'] and not result['needs_new_owner_grant']
    service.revoke(p['publication_id'], p['request_hash'], 'owner')
    configure(service)
    result = service.approval_requirements(p['publication_id'], 'alice', context())
    assert result['needs_new_owner_grant'] and not result['current_grant_valid']
