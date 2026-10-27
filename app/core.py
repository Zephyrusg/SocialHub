import json
import logging
from datetime import datetime, timezone


class HubError(Exception):
    def __init__(self, code, message, retryable=False, *, diagnostics=None):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.diagnostics = diagnostics or {}
        self.stage = None


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Never interpolate third-party messages: HTTP clients may include query secrets.
        return json.dumps({'time': datetime.now(timezone.utc).isoformat(), 'level': record.levelname,
                           'event': record.msg if record.name == 'socialhub' else 'library_event',
                           'logger': record.name, **getattr(record, 'safe_fields', {})})


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    for name in ('httpx', 'httpcore', 'sqlalchemy.engine'):
        logging.getLogger(name).setLevel(logging.WARNING)
