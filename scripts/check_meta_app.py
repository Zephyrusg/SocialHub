"""Validate the configured app against Meta without user-token or publication calls."""
import asyncio
import json
from app.config import Settings
from app.core import configure_logging, HubError
from app.connectors.meta import GraphClient


async def main():
    configure_logging()
    s = Settings()
    try:
        if not s.meta_app_id or not s.meta_app_secret.get_secret_value():
            raise HubError('meta_not_configured', 'Complete local configuration first')
        exchange = GraphClient('', s.meta_graph_version)
        result = await exchange.get('oauth/access_token', {'client_id': s.meta_app_id,
            'client_secret': s.meta_app_secret.get_secret_value(), 'grant_type': 'client_credentials'})
        token = result.get('access_token')
        if not isinstance(token, str) or not token:
            raise HubError('invalid_meta_token', 'Meta returned no app token')
        graph = GraphClient(token, s.meta_graph_version)
        app = await graph.get(s.meta_app_id, {'fields': 'id,name'})
        if str(app.get('id')) != s.meta_app_id:
            raise HubError('account_mismatch', 'Meta app identity mismatch')
        print(json.dumps({'status': 'passed', 'graph_version': s.meta_graph_version, 'app_name': app.get('name'),
            'app_identity_matches_configuration': True, 'account_discovery_verified': False, 'writes_performed': 0}, indent=2))
        return True
    except HubError as error:
        print(json.dumps({'status': 'blocked', 'error': {'code': error.code, 'message': error.message}, 'writes_performed': 0}, indent=2))
        return False
    except Exception:
        print(json.dumps({'status': 'failed', 'error': {'code': 'app_check_failed', 'message': 'Check local app configuration; no credentials displayed'}, 'writes_performed': 0}))
        return False


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
