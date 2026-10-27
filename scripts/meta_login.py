"""Print a one-use login link in a local terminal only; never print API credentials."""
from app.config import Settings
from app.core import HubError, configure_logging
from app.database import database
from app.services.meta_oauth import MetaOAuth

configure_logging()
settings = Settings()
_, sessions = database(settings.database_url.get_secret_value())
try:
    print(MetaOAuth(settings, sessions).issue_login())
    print('Open this local HTTPS link in your browser within 10 minutes. Do not share it.')
except HubError as error:
    raise SystemExit(f'{error.code}: {error.message}') from None
