"""Trusted owner terminal: the only approval issuer. Never an MCP tool or HTTP route."""
import argparse
import getpass
import json
import sys
from app.config import Settings
from app.core import HubError
from app.database import database
from app.services.publishing import PublishingService

p = argparse.ArgumentParser(description='Approve exactly reviewed content after an explicit user request. Does not execute it.')
p.add_argument('publication_id')
args = p.parse_args()
if not sys.stdin.isatty():
    raise SystemExit('Approval requires a trusted interactive owner terminal, not model arguments or piped input')
s = Settings()
_, sessions = database(s.database_url.get_secret_value())
service = PublishingService(s, sessions)
preview = service.review(args.publication_id, None)
print(json.dumps(preview, indent=2))
if not s.meta_live_writes or not s.publishing_enabled:
    raise SystemExit('Live publishing is disabled. No approval issued.')
request = input('Record the explicit user instruction authorizing these exact targets/content: ').strip()
confirmation = input('Review the complete preview above. Type its entire request_hash to approve: ').strip()
try:
    result = service.approve(args.publication_id, confirmation, 'local-owner:' + getpass.getuser(), request)
    print(json.dumps(result))
except HubError as e:
    raise SystemExit(e.code + ': ' + e.message) from None
