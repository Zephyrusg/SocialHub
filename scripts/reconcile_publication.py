"""Trusted owner read-only reconciliation; never reissues an uncertain public write."""
import argparse
import asyncio
import getpass
import json
import sys
from app.config import Settings
from app.core import HubError, configure_logging
from app.database import database
from app.services.publishing import PublishingService

p=argparse.ArgumentParser()
p.add_argument('publication_id')
p.add_argument('target_id')
p.add_argument('remote_id')
a=p.parse_args()
if not sys.stdin.isatty():
    raise SystemExit('Use a trusted interactive owner terminal')
configure_logging()
s=Settings();_,sessions=database(s.database_url.get_secret_value());service=PublishingService(s,sessions)
print(json.dumps(service.review(a.publication_id,None),indent=2))
if input('Type the remote ID after independently inspecting this exact outcome: ').strip()!=a.remote_id:
    raise SystemExit('Reconciliation cancelled')
try:
    print(json.dumps(asyncio.run(service.reconcile(a.publication_id,a.target_id,a.remote_id,'local-owner:'+getpass.getuser())),indent=2))
except HubError as e:
    raise SystemExit(e.code+': '+e.message) from None
