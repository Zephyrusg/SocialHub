"""Short-lived independent owner sessions. No MCP bearer can log into this portal."""
from datetime import timedelta
import hashlib
import hmac
import secrets
from sqlalchemy import select, update, delete
from app.core import HubError
from app.database.models import ApprovalPortalSession, Audit, utcnow


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class OwnerPortal:
    def __init__(self, settings, sessions, clock=utcnow):
        self.settings, self.sessions, self.clock = settings, sessions, clock

    def enabled(self):
        if not self.settings.approval_portal_enabled:
            raise HubError('portal_disabled', 'The optional owner portal is disabled')

    def create_session(self, subject):
        ticket = self.issue_login(subject).split('ticket=', 1)[1]
        return self.start(ticket)

    def issue_login(self, owner):
        """Administrative owner terminal only; never exposed as a tool or HTTP issuer."""
        self.enabled()
        if not owner or len(owner) > 128:
            raise HubError('portal_denied', 'A bounded administrative owner identity is required')
        ticket = secrets.token_urlsafe(32)
        with self.sessions.begin() as db:
            db.execute(delete(ApprovalPortalSession).where(ApprovalPortalSession.expires_at <= self.clock()))
            db.add(ApprovalPortalSession(ticket_hash=token_hash(ticket), owner=owner,
                                        expires_at=self.clock() + timedelta(minutes=5)))
            db.add(Audit(subject=owner, operation='portal_login_issued', outcome='success', details={}))
        return self.settings.browser_origin + '/approval/login?ticket=' + ticket

    def start(self, ticket):
        self.enabled()
        if not isinstance(ticket, str) or not 20 <= len(ticket) <= 128:
            raise HubError('portal_denied', 'A valid one-use owner login is required')
        browser = secrets.token_urlsafe(32)
        with self.sessions.begin() as db:
            item = db.scalar(select(ApprovalPortalSession).where(ApprovalPortalSession.ticket_hash == token_hash(ticket)))
            if not item or item.consumed or item.expires_at <= self.clock():
                raise HubError('portal_denied', 'Owner login expired or already used')
            result = db.execute(update(ApprovalPortalSession).where(ApprovalPortalSession.id == item.id,
                ApprovalPortalSession.consumed.is_(False), ApprovalPortalSession.expires_at > self.clock()).values(
                    consumed=True, browser_hash=token_hash(browser), expires_at=self.clock() + timedelta(minutes=15)))
            if result.rowcount != 1:
                raise HubError('portal_denied', 'Owner login already used')
            db.add(Audit(subject=item.owner, operation='portal_login', outcome='success', details={}))
        return browser

    def authenticate(self, browser):
        self.enabled()
        if not isinstance(browser, str) or not 20 <= len(browser) <= 128:
            raise HubError('portal_denied', 'A separate authenticated owner session is required')
        with self.sessions() as db:
            item = db.scalar(select(ApprovalPortalSession).where(ApprovalPortalSession.browser_hash == token_hash(browser),
                ApprovalPortalSession.consumed.is_(True), ApprovalPortalSession.expires_at > self.clock()))
            if not item:
                raise HubError('portal_denied', 'Owner session expired or unavailable')
            return item.owner

    def csrf(self, browser):
        key = self.settings.encryption_key.get_secret_value().encode()
        return hmac.new(key, ('socialhub-owner-portal-csrf-v1:' + browser).encode(), hashlib.sha256).hexdigest()

    def check_csrf(self, browser, value):
        if not isinstance(value, str) or len(value) != 64 or not value.isascii() or not hmac.compare_digest(self.csrf(browser), value):
            raise HubError('portal_denied', 'Owner form verification failed')

    def logout(self, browser):
        owner = self.authenticate(browser)
        with self.sessions.begin() as db:
            db.execute(update(ApprovalPortalSession).where(ApprovalPortalSession.browser_hash == token_hash(browser)).values(expires_at=self.clock()))
            db.add(Audit(subject=owner, operation='portal_logout', outcome='success', details={}))
