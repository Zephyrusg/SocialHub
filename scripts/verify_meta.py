"""Live read-only verification. Never publishes or logs tokens/secret upstream URLs."""
import asyncio
import json
from app.config import Settings
from app.core import HubError, configure_logging
from app.database import database
from app.services.hub import Hub
from app.services.meta_oauth import MetaOAuth
from app.connectors.meta_policy import PAGE_ID, INSTAGRAM_ID
from app.services.meta_diagnostics import latest_failure


async def main():
    configure_logging()
    settings = Settings()
    _, sessions = database(settings.database_url.get_secret_value())
    oauth = MetaOAuth(settings, sessions)
    results = {'graph_version': settings.meta_graph_version, 'writes_performed': 0}
    try:
        results['discovery_and_validation'] = await oauth.revalidate()
        if not settings.meta_live_reads:
            raise HubError('live_reads_disabled', 'Set META_LIVE_READS=true in local configuration and recreate the container')
        hub = Hub(sessions, settings)
        accounts = (await hub.read('list_accounts'))['accounts']
        assert {a['id'] for a in accounts} == {'pixelpapa-facebook', 'pixelpapa-instagram'}
        results['accounts'] = accounts
        results['posts'], results['capabilities'] = {}, {}
        for aid in ['pixelpapa-facebook', 'pixelpapa-instagram']:
            page = await hub.read('list_posts', aid, limit=25, cursor=None)
            results['posts'][aid] = {'count_first_page': len(page['items']), 'has_more': bool(page['next_cursor'])}
            if page['next_cursor']:
                next_page = await hub.read('list_posts', aid, limit=25, cursor=page['next_cursor'])
                results['posts'][aid]['count_second_page'] = len(next_page['items'])
            results['capabilities'][aid] = await hub.read('get_account_capabilities', aid)
        results['status'] = 'passed'
    except HubError as error:
        results.update(status='blocked', error={'code': error.code, 'message': error.message, 'stage': error.stage, **error.diagnostics}, last_oauth_failure=latest_failure(sessions))
    except Exception:
        results.update(status='failed', error={'code': 'verification_failed', 'message': 'Inspect local readiness and account configuration; no secrets displayed'})
    print(json.dumps(results, indent=2))
    return results['status'] == 'passed'


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
