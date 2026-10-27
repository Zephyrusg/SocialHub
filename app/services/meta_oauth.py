"""Facebook Login onboarding, verification and encrypted account connection."""
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from urllib.parse import urlencode
from sqlalchemy import select, update, delete
from app.connectors.meta import GraphClient, FacebookConnector, InstagramConnector
from app.connectors.meta_policy import PAGE_ID, INSTAGRAM_ID, INSTAGRAM_USERNAME, REQUIRED_READ, REQUESTED_SCOPES, capabilities
from app.core import HubError
from app.database.models import Account, Capability, Credential, OAuthFlow, Audit, utcnow
from app.security.tokens import TokenVault
from app.services.meta_diagnostics import STAGE, diagnose, step


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def expiration(*timestamps):
    dates = [datetime.fromtimestamp(float(v), timezone.utc).replace(tzinfo=None) for v in timestamps if v and float(v) > 0]
    return min(dates) if dates else None


class MetaOAuth:
    def __init__(self, settings, sessions, transport=None):
        self.settings, self.sessions, self.transport = settings, sessions, transport
        self.vault = TokenVault([settings.encryption_key.get_secret_value()])

    def configured(self):
        if not self.settings.meta_app_id.isdigit() or not self.settings.meta_app_secret.get_secret_value():
            raise HubError('meta_not_configured', 'Run scripts/configure_meta.py locally and recreate the application container')

    def graph(self, token, app_proof=True):
        return GraphClient(token, self.settings.meta_graph_version, transport=self.transport,
                           app_secret=self.settings.meta_app_secret.get_secret_value() if app_proof else '')

    def issue_login(self):
        self.configured()
        ticket = secrets.token_urlsafe(32)
        with self.sessions.begin() as db:
            db.execute(delete(OAuthFlow).where(OAuthFlow.expires_at < utcnow()))
            db.add(OAuthFlow(ticket_hash=digest(ticket), expires_at=utcnow() + timedelta(minutes=10)))
        return self.settings.browser_origin + '/meta/oauth/start?' + urlencode({'ticket': ticket})

    def start(self, ticket):
        self.configured()
        if not ticket or len(ticket) > 128:
            raise HubError('invalid_oauth_flow', 'Invalid or expired local login link')
        state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.sessions.begin() as db:
            result = db.execute(update(OAuthFlow).where(OAuthFlow.ticket_hash == digest(ticket), OAuthFlow.started.is_(False), OAuthFlow.expires_at > utcnow()).values(started=True, state_hash=digest(state), browser_hash=digest(browser)))
            if result.rowcount != 1:
                raise HubError('invalid_oauth_flow', 'Login link expired or already used; issue a new local link')
        params = {'client_id': self.settings.meta_app_id, 'redirect_uri': self.settings.meta_redirect_uri,
                  'response_type': 'code', 'state': state, 'scope': ','.join(REQUESTED_SCOPES), 'auth_type': 'rerequest'}
        return 'https://www.facebook.com/' + self.settings.meta_graph_version + '/dialog/oauth?' + urlencode(params), browser

    def consume(self, state, browser):
        if not state or not browser or len(state) > 128 or len(browser) > 128:
            raise HubError('invalid_oauth_state', 'Missing browser-bound OAuth state; start a new local login')
        with self.sessions.begin() as db:
            result = db.execute(update(OAuthFlow).where(OAuthFlow.state_hash == digest(state), OAuthFlow.browser_hash == digest(browser), OAuthFlow.started.is_(True), OAuthFlow.consumed.is_(False), OAuthFlow.expires_at > utcnow()).values(consumed=True))
            if result.rowcount != 1:
                raise HubError('invalid_oauth_state', 'OAuth state expired, mismatched or replayed; start a new login')

    @diagnose
    async def complete(self, state, browser, code, denied=False):
        self.configured()
        STAGE.set('callback_validation')
        self.consume(state, browser)  # Consume before any network request, including a denied login.
        if denied:
            raise HubError('oauth_denied', 'Facebook Login was declined; no account credentials were changed')
        if not code or len(code) > 8192:
            raise HubError('invalid_oauth_code', 'Facebook authorization code is missing or invalid')
        s = self.settings
        short = await step(self, 'authorization_code_exchange', self.graph('', False).get('oauth/access_token', {'client_id': s.meta_app_id,
            'client_secret': s.meta_app_secret.get_secret_value(), 'redirect_uri': s.meta_redirect_uri, 'code': code}))
        short_token = short.get('access_token')
        if not isinstance(short_token, str) or not short_token:
            raise HubError('invalid_api_response', 'Meta code exchange returned no token')
        long = await step(self, 'long_lived_token_exchange', self.graph('', False).get('oauth/access_token', {'grant_type': 'fb_exchange_token',
            'client_id': s.meta_app_id, 'client_secret': s.meta_app_secret.get_secret_value(), 'fb_exchange_token': short_token}))
        token = long.get('access_token')
        if not isinstance(token, str) or not token:
            raise HubError('invalid_api_response', 'Meta long-lived exchange returned no token')
        expires_in = long.get('expires_in')
        exchange_expiry = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).timestamp() if expires_in else None
        return await self.connect(token, exchange_expiry)

    async def debug(self, token, expected_type):
        app_token = self.settings.meta_app_id + '|' + self.settings.meta_app_secret.get_secret_value()
        payload = await self.graph(app_token, False).get('debug_token', {'input_token': token})
        data = payload.get('data', {})
        if not isinstance(data, dict) or data.get('is_valid') is not True or str(data.get('app_id')) != self.settings.meta_app_id:
            raise HubError('invalid_meta_token', 'Meta token is invalid or belongs to a different app; reconnect')
        if data.get('type') != expected_type:
            raise HubError('invalid_meta_token', 'Meta token has an unexpected token type')
        expiry = expiration(data.get('expires_at'), data.get('data_access_expires_at'))
        if expiry and expiry <= utcnow():
            raise HubError('token_expired', 'Meta token or data-access grant expired; reconnect')
        for item in data.get('granular_scopes', []):
            targets = item.get('target_ids', [])
            if targets and item.get('scope') in REQUIRED_READ and not ({PAGE_ID, INSTAGRAM_ID} & set(map(str, targets))):
                raise HubError('permission_denied', 'Meta granular scope does not include the authorized PixelPapa accounts')
        return data

    @diagnose
    async def connect(self, user_token, exchange_expiry=None):
        """Read-only Graph verification; atomic DB connection only after every check succeeds."""
        user_debug = await step(self, 'user_token_validation', self.debug(user_token, 'USER'))
        graph = self.graph(user_token)
        granted, cursor, seen = set(), None, set()
        for _ in range(100):
            params = {'limit': 100}
            if cursor:
                params['after'] = cursor
            page = FacebookConnector.page(await step(self, 'permission_discovery', graph.get('me/permissions', params)))
            granted.update(v.get('permission') for v in page['items'] if v.get('status') == 'granted')
            cursor = page['next_cursor']
            if not cursor:
                break
            if cursor in seen:
                raise HubError('invalid_api_response', 'Meta returned repeated permission cursors')
            seen.add(cursor)
        else:
            raise HubError('lookup_limit', 'Permission discovery exceeded 100 pages')
        granted &= set(user_debug.get('scopes', []))
        missing = REQUIRED_READ - granted
        if missing:
            raise HubError('permission_denied', 'Missing granted read permissions: ' + ', '.join(sorted(missing)))
        page_record, cursor, seen = None, None, set()
        discovered = []
        for _ in range(100):
            # Only the authorized Page ID is retained; other account names/IDs are never returned or persisted.
            params = {'fields': 'id,name,tasks,instagram_business_account', 'limit': 100}
            if cursor:
                params['after'] = cursor
            page = FacebookConnector.page(await step(self, 'page_discovery', graph.get('me/accounts', params)))
            for item in page['items']:
                if str(item.get('id')) == PAGE_ID:
                    page_record = item
                    discovered = [PAGE_ID]
            cursor = page['next_cursor']
            if not cursor:
                break
            if cursor in seen:
                raise HubError('invalid_api_response', 'Meta returned repeated Page cursors')
            seen.add(cursor)
        else:
            raise HubError('lookup_limit', 'Page discovery exceeded 100 pages')
        if not page_record:
            raise HubError('account_denied', 'PixelPapa Page was not returned by Facebook; select it in Login and verify Page access')
        linked = page_record.get('instagram_business_account') or {}
        if str(linked.get('id')) != INSTAGRAM_ID:
            raise HubError('account_mismatch', 'PixelPapa Page is not linked to the authorized Instagram ID')
        tasks = page_record.get('tasks', [])
        if not isinstance(tasks, list):
            raise HubError('invalid_api_response', 'Meta returned invalid Page tasks')
        if not capabilities('facebook', granted, tasks, 'page')['list_posts']:
            raise HubError('permission_denied', 'Page tasks do not permit reading; verify the owner Page role/tasks')
        token_data = await step(self, 'page_token_retrieval', graph.get(PAGE_ID, {'fields': 'id,access_token'}))
        page_token = token_data.get('access_token')
        if str(token_data.get('id')) != PAGE_ID or not isinstance(page_token, str) or not page_token:
            raise HubError('invalid_meta_token', 'Meta did not return the expected Page access token')
        page_debug = await step(self, 'page_token_validation', self.debug(page_token, 'PAGE'))
        page_scopes = granted & set(page_debug.get('scopes', []))
        if REQUIRED_READ - page_scopes:
            raise HubError('permission_denied', 'Page token is missing required granted permissions; reconnect')
        page_graph = self.graph(page_token)
        verified_page = await step(self, 'page_identity_verification', page_graph.get('me', {'fields': 'id,name,instagram_business_account'}))
        if str(verified_page.get('id')) != PAGE_ID or str((verified_page.get('instagram_business_account') or {}).get('id')) != INSTAGRAM_ID:
            raise HubError('account_mismatch', 'Page token identity/link did not match the authorized accounts')
        ig = await step(self, 'instagram_account_discovery', page_graph.get(INSTAGRAM_ID, {'fields': 'id,username'}))
        if str(ig.get('id')) != INSTAGRAM_ID or ig.get('username', '').lower() != INSTAGRAM_USERNAME:
            raise HubError('account_mismatch', 'Instagram identity did not match the authorized account')
        # Facebook Login's IGUser has no account_type field. The verified Page link
        # establishes professional eligibility; Creator/Business subtype is owner-declared.
        kind = self.settings.meta_instagram_account_type
        kind_source = 'owner_declared'
        if ig.get('account_type') is not None:
            # Accept a subtype only if the API actually supplies it; never request
            # the Instagram-Login-only field through Facebook Graph.
            kind = {'MEDIA_CREATOR': 'creator', 'BUSINESS': 'business'}.get(str(ig['account_type']).upper())
            if kind is None:
                raise HubError('unsupported_account_type', 'Instagram must be a Professional Creator or Business account')
            kind_source = 'api_returned'
        expiry = expiration(exchange_expiry, user_debug.get('expires_at'), user_debug.get('data_access_expires_at'), page_debug.get('expires_at'), page_debug.get('data_access_expires_at'))
        STAGE.set('credential_storage')
        with self.sessions.begin() as db:
            # Authorization is for exactly these real accounts. Existing mocks remain for regression tests but are hidden in live mode.
            for platform, remote, name, account_type in [('facebook', PAGE_ID, verified_page.get('name', 'PixelPapa'), 'page'), ('instagram', INSTAGRAM_ID, ig['username'], kind)]:
                aid = f'pixelpapa-{platform}'
                account = db.get(Account, aid)
                if not account:
                    account = Account(id=aid, platform=platform, remote_id=remote, name=name, account_type=account_type, allowlisted=True, is_mock=False)
                    db.add(account)
                    db.flush()
                if account.remote_id != remote or account.platform != platform or account.is_mock:
                    raise HubError('account_mismatch', 'Stored account identity conflicts with PixelPapa')
                account.name, account.account_type, account.allowlisted = name, account_type, True
                encrypted = {'access_token': page_token, 'tasks': tasks, 'page_id': PAGE_ID,
                             'checked_at': utcnow().isoformat() + 'Z', 'account_type_source': kind_source if platform == 'instagram' else 'page_identity'}
                if platform == 'facebook':
                    encrypted['user_access_token'] = user_token
                credential = db.scalar(select(Credential).where(Credential.account_id == aid))
                if not credential:
                    credential = Credential(account_id=aid, ciphertext='', scopes=[])
                    db.add(credential)
                credential.ciphertext = self.vault.encrypt(encrypted)
                credential.scopes, credential.expires_at = sorted(page_scopes), expiry
                credential.refreshed_at, credential.revoked = utcnow(), False
                caps = capabilities(platform, page_scopes, tasks, account_type)
                if platform == 'instagram' and kind_source != 'api_returned':
                    caps['stories'] = False
                for operation, supported in caps.items():
                    entry = db.get(Capability, (aid, operation))
                    if entry is None:
                        entry = Capability(account_id=aid, operation=operation, supported=supported, details={})
                        db.add(entry)
                    entry.supported = supported
                    entry.details = {'source': 'meta_verified', 'checked_at': utcnow().isoformat() + 'Z'}
            db.add(Audit(subject='local-meta-owner', operation='meta_connect', account_id=PAGE_ID, outcome='success', details={'accounts': [PAGE_ID, INSTAGRAM_ID], 'scopes': sorted(page_scopes)}))
        return {'connected': [PAGE_ID, INSTAGRAM_ID], 'discovered_authorized_pages': discovered,
                'permissions': sorted(page_scopes), 'page_tasks': tasks, 'instagram_type': kind, 'instagram_type_source': kind_source,
                'expires_at': expiry.isoformat() + 'Z' if expiry else None, 'live_writes_enabled': False}

    @diagnose
    async def revalidate(self):
        self.configured()
        STAGE.set('stored_credentials')
        with self.sessions() as db:
            credential = db.scalar(select(Credential).where(Credential.account_id == 'pixelpapa-facebook'))
            if not credential:
                raise HubError('meta_not_connected', 'Complete the local Facebook Login flow first')
            # Explicitly refuses expired credentials: Facebook Login must be repeated, not a fictitious refresh_token flow.
            user_token = self.vault.decrypt(credential).get('user_access_token')
        if not user_token:
            raise HubError('reauthorization_required', 'Stored user token unavailable; reconnect with Facebook Login')
        try:
            return await self.connect(user_token)
        except HubError as error:
            if error.code in {'token_expired', 'invalid_meta_token', 'permission_denied', 'account_mismatch'}:
                with self.sessions.begin() as db:
                    for aid in ['pixelpapa-facebook', 'pixelpapa-instagram']:
                        saved = db.scalar(select(Credential).where(Credential.account_id == aid))
                        if saved:
                            saved.revoked = True
                    db.add(Audit(subject='local-meta-owner', operation='meta_revalidate', outcome='denied', details={'code': error.code}))
            raise
