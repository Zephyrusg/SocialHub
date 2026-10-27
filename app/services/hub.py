import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from app.core import HubError
from app.connectors.mock import MockConnector
from app.connectors.meta import GraphClient, FacebookConnector, InstagramConnector
from app.connectors.meta_policy import PAGE_ID, INSTAGRAM_ID, capabilities
from app.database.models import Credential
from app.security.tokens import TokenVault
from app.services.administration import EffectiveSettings, effective_settings
from app.database.models import Account, Capability, Post, Job, Idempotency, Audit, Operation


class Hub:
    def __init__(self, sessions, settings=None):
        self.sessions = sessions
        self.settings = settings
        self.vault = TokenVault([settings.encryption_key.get_secret_value()]) if settings else None
        self.connectors = {p: MockConnector(p) for p in ['facebook', 'instagram']}

    @property
    def live_reads(self):
        return bool(self.settings and self.settings.meta_live_reads)

    def seed_mocks(self):
        with self.sessions.begin() as db:
            for platform, kind in [('facebook', 'page'), ('instagram', 'creator')]:
                aid = f'mock-{platform}'
                if db.get(Account, aid):
                    continue
                account = Account(id=aid, platform=platform, remote_id=f'mock_{platform}', name='PixelPapa (MOCK)', account_type=kind, allowlisted=True, is_mock=True)
                db.add(account)
                db.flush()
                for op, supported in self.connectors[platform].capabilities(kind).items():
                    db.add(Capability(account_id=aid, operation=op, supported=supported, details={'source': 'mock_fixture'}))

    def account(self, db, account_id):
        account = db.get(Account, account_id)
        if not account or not account.allowlisted:
            raise HubError('account_denied', 'Account is not allowlisted')
        if account.is_mock and self.live_reads:
            raise HubError('account_denied', 'Mock accounts are disabled while live reads are enabled')
        if not account.is_mock:
            expected = {'facebook': PAGE_ID, 'instagram': INSTAGRAM_ID}.get(account.platform)
            if not self.live_reads:
                raise HubError('live_disabled', 'Enable verified live reads after local Meta configuration')
            if account.remote_id != expected or account.id != f'pixelpapa-{account.platform}':
                raise HubError('account_denied', 'Only the two verified PixelPapa accounts are authorized')
        if account.platform not in self.connectors:
            raise HubError('unsupported_platform', 'No connector registered for this platform')
        return account

    async def read(self, operation, account_id=None, **args):
        if isinstance(self.settings, EffectiveSettings):
            # Freeze setup once before acquiring an operation DB connection.
            # Nested dynamic lookups could exhaust the small production pool.
            return await Hub(self.sessions, effective_settings(self.settings, self.sessions)).read(operation, account_id, **args)
        with self.sessions() as db:
            if operation == 'get_system_status':
                db.execute(text('SELECT 1'))
                return {'status': 'ok', 'database': 'ok', 'mode': 'live_guarded_publishing' if self.settings and self.settings.meta_live_writes and self.settings.publishing_enabled else 'live_read_only' if self.live_reads else 'mock', 'live_writes_enabled': bool(self.settings and self.settings.meta_live_writes and self.settings.publishing_enabled)}
            if operation == 'list_accounts':
                return {'accounts': [{'id': a.id, 'name': a.name, 'platform': a.platform, 'account_type': a.account_type, 'mock': a.is_mock} for a in db.scalars(select(Account).where(Account.allowlisted.is_(True), Account.is_mock.is_(not self.live_reads))) if not self.live_reads or (a.id == f'pixelpapa-{a.platform}' and a.remote_id == {'facebook': PAGE_ID, 'instagram': INSTAGRAM_ID}.get(a.platform))]}
            account = self.account(db, account_id)
            if account.is_mock:
                if self.live_reads:
                    raise HubError('account_denied', 'Mock accounts are hidden while live reads are enabled')
                connector = self.connectors[account.platform]
                caps = connector.capabilities(account.account_type)
                checked_at = None
                account_type_source = 'fixture'
            else:
                credential = db.scalar(select(Credential).where(Credential.account_id == account.id))
                if not credential:
                    raise HubError('meta_not_connected', 'Complete local Facebook Login before reading this account')
                stored = self.vault.decrypt(credential)
                graph = GraphClient(stored['access_token'], self.settings.meta_graph_version,
                    app_secret=self.settings.meta_app_secret.get_secret_value(), allow_writes=False)
                cls = FacebookConnector if account.platform == 'facebook' else InstagramConnector
                connector = cls(graph, account.remote_id)
                caps = capabilities(account.platform, credential.scopes, stored.get('tasks', []), account.account_type)
                checked_at = stored.get('checked_at')
                account_type_source = stored.get('account_type_source', 'unknown')
                caps['write_account_allowed'] = account.id in self.settings.meta_write_account_ids
                caps['live_writes_enabled'] = bool(caps['write_account_allowed'] and self.settings.meta_live_writes and self.settings.publishing_enabled)
                if account.platform == 'instagram' and account_type_source != 'api_returned':
                    caps['stories'] = False
            if operation == 'get_account_capabilities':
                return {'account_id': account.id, 'mock': account.is_mock, 'capabilities': caps, 'verified_at': checked_at, 'account_type_source': account_type_source,
                        'limitations': ['Facebook updates limited to supported post fields/types', 'Instagram post editing unavailable', 'Analytics not implemented'] + ([] if caps.get('live_writes_enabled') else ['Live writes disabled'])}
            if not caps.get(operation):
                raise HubError('unsupported_operation', 'Operation not supported for this account')
            try:
                return await getattr(connector, operation)(account.remote_id, **args)
            except HubError as error:
                if not account.is_mock and error.code == 'token_expired':
                    # Both accounts share a Page token; invalidate the pair consistently.
                    with self.sessions.begin() as changes:
                        for cid in ['pixelpapa-facebook', 'pixelpapa-instagram']:
                            saved = changes.scalar(select(Credential).where(Credential.account_id == cid))
                            if saved:
                                saved.revoked = True
                raise

    def record(self, subject, operation, account_id, outcome, code=None, retryable=False):
        with self.sessions.begin() as db:
            db.add(Audit(subject=subject, operation=operation, account_id=account_id, outcome=outcome, details={'error_code': code} if code else {}))
            db.add(Operation(account_id=account_id, operation=operation, status=outcome, error_code=code, retryable=retryable))

    def write(self, operation, account_id, payload, key, subject, scopes):
        if isinstance(self.settings, EffectiveSettings):
            return Hub(self.sessions, effective_settings(self.settings, self.sessions)).write(operation, account_id, payload, key, subject, scopes)
        # A scope is authorization to exercise a mock operation, not proof of user intent.
        # There is no live-write path or model-supplied approval flag.
        if 'socialhub:mock-write' not in scopes:
            raise HubError('insufficient_scope', 'Mock write scope required')
        digest = hashlib.sha256(json.dumps({'operation': operation, 'account': account_id, 'payload': payload}, sort_keys=True).encode()).hexdigest()
        try:
            with self.sessions.begin() as db:
                account = self.account(db, account_id)
                if not account.is_mock:
                    raise HubError('live_writes_disabled', 'Use the approval-backed publishing service; direct live writes are refused')
                caps = self.connectors[account.platform].capabilities(account.account_type)
                if not caps.get(operation):
                    raise HubError('unsupported_operation', 'Operation not supported; no destructive replacement is performed')
                cached = db.get(Idempotency, key)
                if cached:
                    if cached.subject != subject or cached.request_hash != digest:
                        raise HubError('idempotency_conflict', 'Key is already bound to a different request or principal')
                    return cached.response
                # Claim unique key before creating effects; concurrent requests block on this insert.
                record = Idempotency(key=key, subject=subject, request_hash=digest, response={})
                db.add(record)
                db.flush()
                result = {'id': str(uuid4()), 'operation': operation, 'mock': True, 'account_id': account_id}
                if operation == 'schedule_post':
                    when = self.schedule_time(payload['run_at'], payload['timezone'])
                    db.add(Job(id=result['id'], account_id=account_id, run_at=when,
                               requested_timezone=payload['timezone'], payload=payload, authorization_subject=subject, idempotency_key=key))
                    result.update(run_at=when.replace(tzinfo=timezone.utc).isoformat(), state='pending_mock')
                elif operation == 'publish_post':
                    db.add(Post(id=result['id'], account_id=account_id, remote_id=f"mock_{result['id']}", content=payload))
                elif operation == 'update_post':
                    post = db.scalar(select(Post).where(Post.account_id == account_id, Post.remote_id == payload['post_id']))
                    if not post:
                        raise HubError('post_not_found', 'Only locally created mock posts can be updated')
                    post.content = {'text': payload['text']}
                    result['id'] = post.id
                elif operation == 'reply_to_comment':
                    expected = f'{account.remote_id}_sample_comment'
                    if payload['comment_id'] != expected:
                        raise HubError('comment_not_found', 'Comment does not belong to this mock account')
                else:
                    raise HubError('unsupported_operation', 'Unknown write operation')
                record.response = result
                db.add(Audit(subject=subject, operation=operation, account_id=account_id, outcome='mock_success', details={'result_id': result['id']}))
                return result
        except IntegrityError:
            with self.sessions() as db:
                record = db.get(Idempotency, key)
                if record and record.subject == subject and record.request_hash == digest:
                    return record.response
            raise HubError('idempotency_conflict', 'Concurrent request conflicts with this key') from None

    @staticmethod
    def schedule_time(value, zone):
        try:
            tz = ZoneInfo(zone)
            when = datetime.fromisoformat(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise HubError('invalid_schedule', 'Use an ISO 8601 datetime with offset and an IANA timezone') from None
        if when.tzinfo is None or when.utcoffset() != when.astimezone(tz).utcoffset():
            raise HubError('invalid_schedule', 'Explicit offset must agree with the requested timezone, including DST')
        when = when.astimezone(timezone.utc)
        if when <= datetime.now(timezone.utc):
            raise HubError('invalid_schedule', 'Schedule must be in the future')
        return when.replace(tzinfo=None)
