"""Account policy must stop writes even with valid owner grants and global flags."""
import pytest
from sqlalchemy import select
from app.config import Settings
from app.core import HubError
from app.database.models import PublicationTarget
from app.services.approval_policy import ApprovalPolicy
from test_publishing import publisher, prepare, approve
from test_approval_policy import native_settings, context


async def test_empty_allowlist_denies_preparation_and_adapter(publisher):
    service, _, calls, _ = publisher
    service.settings.meta_write_account_ids = []
    with pytest.raises(HubError) as error:
        await prepare(service)
    assert error.value.code == 'write_account_denied'
    with pytest.raises(HubError):
        service.context('pixelpapa-facebook', execute=True)
    assert not calls


async def test_facebook_only_preview_still_requires_owner_and_flags(publisher):
    service, _, calls, _ = publisher
    service.settings.meta_write_account_ids = ['pixelpapa-facebook']
    preview = await prepare(service)
    assert preview['requires_owner_approval']
    with pytest.raises(HubError) as error:
        await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert error.value.code == 'authorization_required'
    approve(service, preview)
    service.settings.meta_live_writes = False
    with pytest.raises(HubError) as error:
        await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert error.value.code == 'live_writes_disabled' and not calls
    service.settings.meta_live_writes = True
    assert (await service.execute('alice', ['socialhub:publish'], preview['publication_id']))['state'] == 'in_progress'
    assert calls[0][0] == 'facebook'


@pytest.mark.parametrize('operation', ['publish', 'update', 'reply'])
async def test_instagram_denied_before_platform_validation(publisher, operation):
    service, _, calls, _ = publisher
    service.settings.meta_write_account_ids = ['pixelpapa-facebook']
    args = {} if operation == 'publish' else {'post_id': 'owned_post'}
    if operation == 'reply':
        args['comment_id'] = 'owned_comment'
    with pytest.raises(HubError) as error:
        await prepare(service, accounts=['pixelpapa-instagram'], operation=operation, **args)
    assert error.value.code == 'write_account_denied' and not calls


async def test_existing_mixed_approval_cannot_partially_dispatch(publisher, sessions):
    service, adapters, calls, _ = publisher
    # A previously permissible mixed request must be checked in its entirety.
    async def validate(*args):
        pass
    adapters['instagram'].validate = validate
    preview = await prepare(service, accounts=['pixelpapa-facebook', 'pixelpapa-instagram'])
    approve(service, preview)
    service.settings.meta_write_account_ids = ['pixelpapa-facebook']
    with pytest.raises(HubError) as error:
        await service.execute('alice', ['socialhub:publish'], preview['publication_id'])
    assert error.value.code == 'write_account_denied' and not calls
    with sessions() as db:
        targets = list(db.scalars(select(PublicationTarget)))
        assert all(t.state == 'prepared' and t.attempts == 0 for t in targets)
    with pytest.raises(HubError):
        service.claim(preview['publication_id'], preview['targets'][0]['target_id'], 'alice')


async def test_account_removed_after_preview_cannot_gain_owner_grant(publisher):
    service, _, calls, _ = publisher
    preview = await prepare(service)
    service.settings.meta_write_account_ids = []
    with pytest.raises(HubError) as error:
        approve(service, preview)
    assert error.value.code == 'write_account_denied' and not calls


async def test_native_policy_rechecks_accounts_and_fingerprint(publisher):
    service, _, _, _ = publisher
    preview = await prepare(service)
    settings = native_settings(service)
    policy = ApprovalPolicy(settings)
    assert policy.eligible('alice', preview['preview'], context())
    old = policy.fingerprint()
    settings.meta_write_account_ids = []
    assert not policy.eligible('alice', preview['preview'], context())
    assert policy.fingerprint() != old


async def test_settings_account_allowlist_fails_closed(publisher):
    settings = publisher[0].settings
    data = settings.model_dump()
    data.pop('meta_write_account_ids')
    assert Settings.model_validate(data).meta_write_account_ids == []
    with pytest.raises(ValueError):
        Settings.model_validate(settings.model_dump() | {'meta_write_account_ids': ['other-facebook']})


async def test_capabilities_report_facebook_only_without_hiding_instagram_reads(publisher, sessions):
    from app.services.hub import Hub
    service, _, _, _ = publisher
    service.settings.meta_write_account_ids = ['pixelpapa-facebook']
    hub = Hub(sessions, service.settings)
    facebook = (await hub.read('get_account_capabilities', 'pixelpapa-facebook'))['capabilities']
    instagram = (await hub.read('get_account_capabilities', 'pixelpapa-instagram'))['capabilities']
    assert facebook['write_account_allowed'] and facebook['live_writes_enabled']
    assert not instagram['write_account_allowed'] and not instagram['live_writes_enabled']
    assert instagram['list_posts'] and instagram['get_post']
