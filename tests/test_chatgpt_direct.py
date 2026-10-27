"""Explicit operator-selected chat directive trust; never fake a native receipt."""
import pytest
from sqlalchemy import select
from app.core import HubError
from app.database.models import PublicationApproval, Audit
from app.services.approval_policy import ApprovalPolicy
from test_publishing import publisher, prepare
from test_approval_policy import native_settings, context


def configure(service, **changes):
    service.settings = native_settings(service, approval_mode='chatgpt_direct',
        chatgpt_confirmation_tested=False, chatgpt_confirmation_evidence_sha256='',
        chatgpt_direct_tested=True, chatgpt_direct_evidence_sha256='d'*64,
        meta_write_account_ids=['pixelpapa-facebook'], chatgpt_allowed_operations=['publish'], **changes)
    service.approval_policy = ApprovalPolicy(service.settings)


async def test_directive_uses_shared_state_without_claiming_native_or_owner_consent(publisher, sessions):
    service, _, calls, _ = publisher
    configure(service)
    p = await prepare(service)
    requirement = service.approval_requirements(p['publication_id'], 'alice', context())
    assert not requirement['independent_owner_required']
    assert requirement['consent_basis'] == 'authenticated_chat_directive_reliance'
    assert requirement['direct_instruction_test_recorded']
    assert not requirement['native_host_test_recorded'] and not requirement['native_confirmation_receipt_verified']
    assert (await service.execute('alice', ['socialhub:publish'], p['publication_id'], context()))['state'] == 'in_progress'
    result = await service.execute('alice', ['socialhub:publish'], p['publication_id'], context())
    assert result['state'] == 'succeeded' and not result['independent_owner_approval']
    await service.execute('alice', ['socialhub:publish'], p['publication_id'], context())
    assert len(calls) == 2
    with sessions() as db:
        assert db.scalar(select(PublicationApproval)).channel == 'chatgpt_policy'
        event = db.scalar(select(Audit).where(Audit.operation == 'authorize_publication_policy'))
        assert event.details['approval_mode'] == 'chatgpt_direct'
        assert event.details['native_confirmation_verified'] is False
        assert event.details['independent_consent_verified'] is False


@pytest.mark.parametrize('change', [
    {'chatgpt_direct_tested':False}, {'chatgpt_direct_evidence_sha256':''},
    {'chatgpt_direct_evidence_sha256':'z'*64}, {'chatgpt_residual_risk_accepted':False},
    {'chatgpt_allowed_client_ids':[]}, {'chatgpt_allowed_principals':[]},
    {'chatgpt_allowed_operations':[]}, {'auth_mode':'local'}])
async def test_direct_startup_requires_explicit_configuration_and_real_evidence(publisher, change):
    service, _, _, _ = publisher
    configure(service)
    from app.config import Settings
    with pytest.raises(ValueError):
        Settings.model_validate(service.settings.model_dump() | change)


@pytest.mark.parametrize('ctx', [None, {'approved':True}, context(oauth_client_id='impostor'),
    context(client_id='other'), context(scopes=['socialhub:read']), context(expires_at=1),
    context(authentication_method='local')])
async def test_direct_mode_cannot_accept_model_claims_or_other_callers(publisher, ctx):
    service, _, calls, _ = publisher
    configure(service)
    p = await prepare(service)
    with pytest.raises(HubError):
        await service.execute('alice', ['socialhub:publish'], p['publication_id'], ctx)
    assert not calls


async def test_direct_mode_retains_operation_account_and_global_gates(publisher):
    service, _, calls, _ = publisher
    configure(service)
    policy = service.approval_policy
    for operation, accounts in [('update',['pixelpapa-facebook']), ('reply',['pixelpapa-facebook']),
        ('publish',['pixelpapa-instagram']), ('publish',['pixelpapa-facebook','pixelpapa-instagram'])]:
        assert not policy.eligible('alice', {'request':{'operation':operation,'account_ids':accounts}}, context())
    p = await prepare(service)
    service.settings.meta_live_writes = False
    with pytest.raises(HubError) as error:
        await service.execute('alice', ['socialhub:publish'], p['publication_id'], context())
    assert error.value.code == 'live_writes_disabled' and not calls


async def test_switching_back_to_owner_policy_stops_inflight_direct_grants(publisher):
    service, _, calls, _ = publisher
    configure(service)
    p = await prepare(service)
    await service.execute('alice', ['socialhub:publish'], p['publication_id'], context())
    service.settings.approval_mode = 'owner_required'
    with pytest.raises(HubError):
        await service.execute('alice', ['socialhub:publish'], p['publication_id'], context())
    assert len(calls) == 1


async def test_native_and_direct_test_evidence_are_not_interchangeable(publisher):
    service, _, _, _ = publisher
    configure(service)
    payload={'request':{'operation':'publish','account_ids':['pixelpapa-facebook']}}
    policy=service.approval_policy
    assert policy.eligible('alice',payload,context())
    service.settings.approval_mode='chatgpt_native'
    assert not policy.eligible('alice',payload,context())
    service.settings.approval_mode='chatgpt_direct'
    service.settings.chatgpt_direct_evidence_sha256='z'*64
    assert not policy.eligible('alice',payload,context())
