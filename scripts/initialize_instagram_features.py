"""Approved-rollout helper: persist off before raising deployment ceilings; never deploys."""
import argparse
from app.config import Settings
from app.database import database
from app.core import HubError
from app.services.feature_flags import FeatureFlags


def initialize(settings, sessions, subject):
    flags = FeatureFlags(settings, sessions)
    for name in ('instagram_photos', 'instagram_story_artwork'):
        status = flags.status(subject)
        row = next(r for r in status['features'] if r['name'] == name)
        if row['configured'] is True:
            raise HubError('rollout_conflict', 'An Instagram preference is already on; review configuration before rollout')
        if row['configured'] is None:
            flags.save(subject, status['revision'], name, False)
    return flags.status(subject)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--administrator', required=True, help='Existing authorized administrator subject')
    args = parser.parse_args()
    settings = Settings()
    _, sessions = database(settings.database_url.get_secret_value())
    status = initialize(settings, sessions, args.administrator)
    print('Instagram preferences explicitly off; configuration revision', status['revision'])
