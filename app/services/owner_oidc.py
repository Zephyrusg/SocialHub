"""Optional approval portal login, independent from administration and MCP."""
from app.services.browser_oidc import BrowserOIDC
from app.services.approval_portal import OwnerPortal


class OwnerOIDC(BrowserOIDC):
    def __init__(self, settings, sessions, transport=None):
        super().__init__(settings, sessions, client_id=settings.owner_oidc_client_id,
            client_secret=settings.owner_oidc_client_secret,
            allowed_subjects=settings.owner_oidc_allowed_subjects,
            callback_path='/approval/oidc/callback', purpose='approval',
            session_backend=OwnerPortal(settings, sessions), enabled=settings.owner_oidc_enabled,
            require_mfa=settings.owner_oidc_require_mfa, transport=transport)
