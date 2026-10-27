"""Exercise real MariaDB atomic duplicate handling with mock effects only."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from sqlalchemy import select, func
from app.config import Settings
from app.database import database
from app.database.models import Post, Audit
from app.services.hub import Hub

s = Settings()
engine, sessions = database(s.database_url.get_secret_value())
hub = Hub(sessions)
key = 'db-concurrency-' + uuid4().hex

def publish(_):
    return hub.write('publish_post', 'mock-facebook', {'text': 'MariaDB concurrent mock test'}, key, 'database-test', ['socialhub:mock-write'])

with ThreadPoolExecutor(max_workers=5) as pool:
    results = list(pool.map(publish, range(10)))
ids = {r['id'] for r in results}
assert len(ids) == 1
pid = next(iter(ids))
with sessions() as db:
    assert db.scalar(select(func.count()).select_from(Post).where(Post.id == pid)) == 1
    assert db.scalar(select(func.count()).select_from(Audit).where(Audit.subject == 'database-test', Audit.details['result_id'].as_string() == pid)) == 1
print('PASS: MariaDB 10 concurrent duplicate requests -> one mock post, one transactional write audit, one stable response')
engine.dispose()
