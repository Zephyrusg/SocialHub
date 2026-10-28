"""Existing-identity admin sessions and narrowly scoped encrypted setup preferences."""
from datetime import timedelta
import hashlib
import hmac
import json
import secrets
from cryptography.fernet import InvalidToken
from pydantic import SecretStr
from sqlalchemy import select, update, delete
from app.core import HubError
from app.database.models import AdminSession, SiteConfiguration, OAuthFlow, Credential, Account, Audit, utcnow
from app.security.tokens import TokenVault
from app.services.browser_oidc import BrowserOIDC
from app.services.approval_portal import token_hash
from app.connectors.meta_policy import PAGE_ID, INSTAGRAM_ID
from app.services.feature_flags import DYNAMIC_FIELDS, apply_flags, deployment_settings

FIELDS = {'meta_app_id', 'meta_app_secret', 'meta_live_reads'}


def require_publisher(settings, subject):
    if settings.auth_mode == 'oauth' and subject not in settings.publishing_allowed_subjects:
        raise HubError('publishing_role_required', 'This identity has no publishing permission')


def effective_settings(settings, sessions):
    base = deployment_settings(settings)
    with sessions() as db:
        row = db.get(SiteConfiguration, 'meta')
        revision = row.revision if row else 0
        try:
            values = json.loads(TokenVault([base.encryption_key.get_secret_value()]).cipher.decrypt(row.ciphertext.encode())) if row and row.ciphertext else {}
            if (not isinstance(values, dict) or set(values) - FIELDS
                    or ('meta_live_reads' in values and not isinstance(values['meta_live_reads'], bool))
                    or ('meta_app_id' in values and (not isinstance(values['meta_app_id'], str) or (values['meta_app_id'] and (not values['meta_app_id'].isascii() or not values['meta_app_id'].isdigit()))))
                    or ('meta_app_secret' in values and (not isinstance(values['meta_app_secret'], str) or not 0 <= len(values['meta_app_secret']) <= 256))):
                raise ValueError()
        except (InvalidToken, ValueError, TypeError):
            raise HubError('configuration_invalid', 'Stored setup configuration could not be verified') from None
        if 'meta_app_secret' in values:
            values['meta_app_secret'] = SecretStr(values['meta_app_secret'])
        result = base.model_copy(update=values)
        result._meta_configuration_revision = revision
        return apply_flags(result, base, db)


class EffectiveSettings:
    """Deployment security policy plus fresh, restrictive database preferences."""
    def __init__(self, base, sessions):
        self.base, self.sessions = base, sessions

    def snapshot(self):
        return effective_settings(self.base, self.sessions)

    def __getattr__(self, name):
        if name in DYNAMIC_FIELDS and getattr(self.base, name) is False:
            return False  # A deployment prohibition cannot require or consult an override.
        if name == 'approval_mode' and self.base.approval_mode != 'chatgpt_direct':
            return self.base.approval_mode
        return getattr(self.snapshot() if name in FIELDS | DYNAMIC_FIELDS | {'_feature_states'} else self.base, name)


class AdminSessions:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions

    def enabled(self):
        if not self.settings.admin_enabled:
            raise HubError('admin_disabled', 'Administration is disabled')

    def policy(self):
        s = self.settings
        return hashlib.sha256(json.dumps([s.oauth_issuer, s.admin_oidc_client_id,
            sorted(s.admin_allowed_subjects), 'password-totp-or-passkey-v2', s.browser_origin], separators=(',', ':')).encode()).hexdigest()

    def create_session(self, subject):
        self.enabled()
        if subject not in self.settings.admin_allowed_subjects:
            raise HubError('admin_denied', 'Administrator identity required')
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as db:
            db.execute(delete(AdminSession).where(AdminSession.expires_at <= utcnow()))
            db.add(AdminSession(token_hash=token_hash(token), subject=subject, policy_hash=self.policy(),
                                expires_at=utcnow() + timedelta(minutes=15)))
            db.add(Audit(subject=subject, operation='admin_login', outcome='success', details={}))
        return token

    def authenticate(self, token):
        self.enabled()
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            raise HubError('admin_denied', 'Separate administrator login required')
        with self.sessions() as db:
            row = db.get(AdminSession, token_hash(token))
            if (not row or row.expires_at <= utcnow() or row.policy_hash != self.policy()
                    or row.subject not in self.settings.admin_allowed_subjects):
                raise HubError('admin_denied', 'Administrator session expired or revoked')
            return row.subject

    def csrf(self, token):
        return hmac.new(self.settings.encryption_key.get_secret_value().encode(),
                        ('socialhub-admin-csrf-v1:' + token).encode(), hashlib.sha256).hexdigest()

    def check_csrf(self, token, value):
        if not isinstance(value, str) or len(value) != 64 or not value.isascii() or not hmac.compare_digest(self.csrf(token), value):
            raise HubError('admin_denied', 'Administrator form verification failed')

    def logout(self, token):
        subject = self.authenticate(token)
        with self.sessions.begin() as db:
            db.execute(delete(AdminSession).where(AdminSession.token_hash == token_hash(token)))
            db.add(Audit(subject=subject, operation='admin_logout', outcome='success', details={}))


class AdminOIDC(BrowserOIDC):
    def __init__(self, settings, sessions, transport=None):
        super().__init__(settings, sessions, client_id=settings.admin_oidc_client_id,
            client_secret=settings.admin_oidc_client_secret, allowed_subjects=settings.admin_allowed_subjects,
            callback_path='/admin/oidc/callback', purpose='admin', session_backend=AdminSessions(settings, sessions),
            enabled=settings.admin_enabled, require_mfa=True, max_age=900, allow_passkey_mfa=True, transport=transport)


class Administration:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions
        self.vault = TokenVault([settings.encryption_key.get_secret_value()])

    def _authorize(self, subject):
        if not self.settings.admin_enabled or subject not in self.settings.admin_allowed_subjects:
            raise HubError('admin_denied', 'An authorized administrator is required')

    def status(self, subject):
        self._authorize(subject)
        active = effective_settings(self.settings, self.sessions)
        with self.sessions() as db:
            accounts = []
            for aid, remote in [('pixelpapa-facebook', PAGE_ID), ('pixelpapa-instagram', INSTAGRAM_ID)]:
                a = db.get(Account, aid)
                c = db.scalar(select(Credential).where(Credential.account_id == aid))
                valid = bool(a and a.allowlisted and not a.is_mock and a.remote_id == remote and c
                    and not c.revoked and (not c.expires_at or c.expires_at > utcnow()))
                accounts.append({'account_id': aid, 'connected': valid})
        return {'revision': active._meta_configuration_revision, 'meta_app_id': active.meta_app_id,
            'meta_secret_configured': bool(active.meta_app_secret.get_secret_value()),
            'live_reads': active.meta_live_reads, 'accounts': accounts,
            'administrator': True, 'publishing_permission': subject in self.settings.publishing_allowed_subjects,
            'live_writes': active.meta_live_writes and active.publishing_enabled,
            'approval_mode': active.approval_mode}

    def save(self, subject, revision, *, app_id=None, app_secret=None, live_reads=None):
        self._authorize(subject)
        if app_id is not None and (not isinstance(app_id, str) or not app_id.isascii() or not app_id.isdigit() or len(app_id) > 64):
            raise HubError('invalid_setup', 'Enter the main Facebook App ID')
        if app_secret is not None and (not isinstance(app_secret, str) or not 1 <= len(app_secret) <= 256
                or not app_secret.isascii() or any(c.isspace() or ord(c) < 32 for c in app_secret)):
            raise HubError('invalid_setup', 'Enter a valid App Secret privately')
        if live_reads is not None and not isinstance(live_reads, bool):
            raise HubError('invalid_setup', 'Invalid read setting')
        active = effective_settings(self.settings, self.sessions)
        if app_id and app_id != active.meta_app_id and not app_secret:
            raise HubError('invalid_setup', 'Changing the App ID also requires its matching App Secret')
        with self.sessions.begin() as db:
            row = db.scalar(select(SiteConfiguration).where(SiteConfiguration.id == 'meta').with_for_update())
            if row is None:
                row = SiteConfiguration(id='meta', revision=0, ciphertext=None);db.add(row);db.flush()
            if row.revision != revision or active._meta_configuration_revision != revision:
                raise HubError('configuration_conflict', 'Setup changed; reload the form before saving')
            values = {'meta_app_id': active.meta_app_id, 'meta_app_secret': active.meta_app_secret.get_secret_value(), 'meta_live_reads': active.meta_live_reads}
            changed = (app_id is not None and app_id != active.meta_app_id) or (app_secret is not None and app_secret != values['meta_app_secret'])
            if app_id is not None: values['meta_app_id'] = app_id
            if app_secret is not None: values['meta_app_secret'] = app_secret
            if changed:
                values['meta_live_reads'] = False
                db.execute(update(Credential).where(Credential.account_id.in_(['pixelpapa-facebook', 'pixelpapa-instagram'])).values(revoked=True))
                db.execute(update(OAuthFlow).where(OAuthFlow.consumed.is_(False)).values(consumed=True, expires_at=utcnow()))
            if live_reads is not None:
                if live_reads:
                    # Enabling is local configuration only; no Meta request or write is performed.
                    if changed or not values['meta_app_id'] or not values['meta_app_secret']:
                        raise HubError('meta_not_connected', 'Connect and verify both accounts first')
                    for aid, remote in [('pixelpapa-facebook', PAGE_ID), ('pixelpapa-instagram', INSTAGRAM_ID)]:
                        a = db.get(Account, aid);c = db.scalar(select(Credential).where(Credential.account_id == aid))
                        if not (a and a.allowlisted and not a.is_mock and a.remote_id == remote and c and not c.revoked
                                and (not c.expires_at or c.expires_at > utcnow())):
                            raise HubError('meta_not_connected', 'Connect and verify both accounts first')
                values['meta_live_reads'] = live_reads
            # No identity, encryption key, host, approval or write flag can be set here.
            row.ciphertext = self.vault.encrypt(values)
            result = db.execute(update(SiteConfiguration).where(SiteConfiguration.id == 'meta', SiteConfiguration.revision == revision).values(revision=revision + 1))
            if result.rowcount != 1:
                raise HubError('configuration_conflict', 'Concurrent setup edit refused')
            db.add(Audit(subject=subject, operation='admin_setup_updated', outcome='success', details={'revision': revision + 1, 'credentials_changed': changed, 'live_reads': values['meta_live_reads']}))
        return revision + 1
