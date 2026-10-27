"""Administrative portal bootstrap. Print the short-lived link only in an owner terminal."""
import getpass
import sys
from app.config import Settings
from app.core import HubError
from app.database import database
from app.services.approval_portal import OwnerPortal

if not sys.stdin.isatty():
    raise SystemExit('A trusted interactive owner terminal is required; no login issued')
s = Settings()
_, sessions = database(s.database_url.get_secret_value())
try:
    print(OwnerPortal(s, sessions).issue_login('owner-portal:' + getpass.getuser()))
    print('Open once within 5 minutes; owner session lasts 15 minutes. Do not share the link.')
except HubError as error:
    raise SystemExit(error.code + ': ' + error.message) from None
