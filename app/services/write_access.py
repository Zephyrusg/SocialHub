"""Explicit account restriction, independent of permissions and owner approval."""
from app.core import HubError


def require_write_accounts(settings, account_ids):
    if not account_ids or any(aid not in settings.meta_write_account_ids for aid in account_ids):
        raise HubError('write_account_denied', 'Publishing is not permitted for every requested account; reads remain available')
