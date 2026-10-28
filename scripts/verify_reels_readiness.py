"""Read-only Reel account/permission assessment. No token refresh, uploads or DB writes."""
import asyncio
import json
from app.config import Settings
from app.core import HubError, configure_logging
from app.database import database
from app.services.publishing import PublishingService
from app.connectors.meta_policy import PAGE_ID, INSTAGRAM_ID, INSTAGRAM_USERNAME


async def assess(service):
    result = {'writes_performed': 0, 'accounts': {}, 'status': 'passed'}
    try:
        page, pcaps, fb = service.context('pixelpapa-facebook', execute=False)
        ig, icaps, instagram = service.context('pixelpapa-instagram', execute=False)
        if fb.graph.allow_writes or instagram.graph.allow_writes:
            raise HubError('verification_failed', 'Read-only adapters are required')
        linked = await fb.read(PAGE_ID, {'fields': 'id,instagram_business_account{id,username}'})
        identity = await instagram.read(INSTAGRAM_ID, {'fields': 'id,username'})
        if (page.remote_id != PAGE_ID or ig.remote_id != INSTAGRAM_ID or str(linked.get('id')) != PAGE_ID
                or str((linked.get('instagram_business_account') or {}).get('id')) != INSTAGRAM_ID
                or str(identity.get('id')) != INSTAGRAM_ID or identity.get('username', '').lower() != INSTAGRAM_USERNAME
                or ig.account_type not in {'creator', 'business'}):
            raise HubError('account_denied', 'The expected linked professional account identities were not verified')
        # Actual GET permission evidence. Cached scopes/tasks are reported separately.
        quota = await instagram.read(f'{INSTAGRAM_ID}/content_publishing_limit', {'fields': 'quota_usage,config'})
        records = quota.get('data') or []
        usage = records[0].get('quota_usage') if records else None
        result['accounts'] = {'facebook': {'identity_verified': True, 'cached_scopes_and_tasks_allow_publish': bool(pcaps.get('publish_post'))},
            'instagram': {'identity_and_page_link_verified': True, 'configured_account_type': ig.account_type,
                'cached_scopes_and_tasks_allow_publish': bool(icaps.get('publish_post')),
                'publishing_limit_read_verified': True, 'quota_usage': usage if type(usage) is int else None}}
        result['resumable_eligibility'] = 'not_verified_until_separately_authorized_session_creation'
        if not pcaps.get('publish_post') or not icaps.get('publish_post'):
            raise HubError('permission_denied', 'Stored permission/task evidence does not allow publication; reauthorize securely')
    except HubError as error:
        result.update(status='blocked', error={'code': error.code, 'message': error.message})
    except Exception:
        result.update(status='blocked', error={'code': 'verification_failed', 'message': 'Read-only account verification failed; no provider exception or credentials displayed'})
    return result


async def main():
    configure_logging()
    settings = Settings()
    _, sessions = database(settings.database_url.get_secret_value())
    result = await assess(PublishingService(settings, sessions))
    print(json.dumps(result, indent=2))
    return result['status'] == 'passed'


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
