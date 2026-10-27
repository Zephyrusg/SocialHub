"""Stage diagnostics contain only application-owned labels and safe error metadata."""
from contextvars import ContextVar
from functools import wraps
import logging
from app.core import HubError
from app.database.models import Audit

STAGE = ContextVar('meta_oauth_stage', default='configuration_validation')


def diagnose(function):
    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        marker = STAGE.set('configuration_validation')
        try:
            return await function(self, *args, **kwargs)
        except HubError as error:
            if error.stage is None:
                error.stage = STAGE.get()
            raise
        finally:
            STAGE.reset(marker)
    return wrapped


async def step(service, name, operation):
    STAGE.set(name)
    try:
        result = await operation
    except HubError as error:
        error.stage = name
        logging.getLogger('socialhub').info('meta_oauth_step', extra={'safe_fields': {'stage': name, 'outcome': 'error', 'code': error.code, **error.diagnostics}})
        raise
    logging.getLogger('socialhub').info('meta_oauth_step', extra={'safe_fields': {'stage': name, 'outcome': 'success'}})
    return result


def latest_failure(sessions):
    from sqlalchemy import select
    with sessions() as db:
        entry = db.scalar(select(Audit).where(Audit.operation == 'meta_connect', Audit.outcome == 'error').order_by(Audit.at.desc()).limit(1))
        if not entry:
            return None
        # Never return arbitrary historic details; allow only fixed diagnostic keys/values.
        details = entry.details or {}
        result = {'at': entry.at.isoformat() + 'Z'}
        for key in ['code', 'stage', 'provider_code', 'provider_subcode', 'reason', 'field', 'node_type']:
            value = details.get(key)
            if isinstance(value, (str, int)):
                result[key] = value
        return result
