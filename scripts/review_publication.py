"""Read a stored preview locally; this command never approves or executes it."""
import argparse
import json
from app.config import Settings
from app.database import database
from app.services.publishing import PublishingService

p = argparse.ArgumentParser()
p.add_argument('publication_id')
args = p.parse_args()
s = Settings()
_, sessions = database(s.database_url.get_secret_value())
print(json.dumps(PublishingService(s, sessions).review(args.publication_id, None), indent=2))
