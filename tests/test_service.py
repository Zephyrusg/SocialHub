from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select, func
from app.core import HubError
from app.database.models import Account, Post, Job, Audit, Idempotency

SCOPES = ['socialhub:mock-write']


def write(hub, key='request-1', account='mock-facebook', subject='tester', text='Hello'):
    return hub.write('publish_post', account, {'text': text}, key, subject, SCOPES)


async def test_read_accounts_and_both_connectors(hub):
    accounts = (await hub.read('list_accounts'))['accounts']
    assert len(accounts) == 2
    for account in accounts:
        result = await hub.read('list_posts', account['id'], limit=1, cursor=None)
        post = result['items'][0]
        assert post['mock']
        assert (await hub.read('get_post', account['id'], post_id=post['id']))['id'] == post['id']
        assert (await hub.read('list_comments', account['id'], post_id=post['id'], limit=1, cursor=None))['items']


async def test_cross_account_read_denied(hub):
    with pytest.raises(HubError, match='not found'):
        await hub.read('get_post', 'mock-facebook', post_id='mock_instagram_sample')


async def test_seed_is_repeatable(hub):
    hub.seed_mocks()
    assert len((await hub.read('list_accounts'))['accounts']) == 2


def test_idempotency(hub, sessions):
    assert write(hub) == write(hub)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Post)) == 1
        assert db.scalar(select(func.count()).select_from(Audit)) == 1


@pytest.mark.parametrize('kwargs', [{'text': 'Different'}, {'subject': 'someone-else'}, {'account': 'mock-instagram'}])
def test_key_conflict(hub, kwargs):
    write(hub)
    with pytest.raises(HubError) as error:
        write(hub, **kwargs)
    assert error.value.code == 'idempotency_conflict'


def test_concurrent_duplicate(hub, sessions):
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: write(hub), range(10)))
    assert len({r['id'] for r in results}) == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Post)) == 1


def test_scope_denied(hub):
    with pytest.raises(HubError) as error:
        hub.write('publish_post', 'mock-facebook', {'text': 'Hi'}, 'key', 'reader', ['socialhub:read'])
    assert error.value.code == 'insufficient_scope'


@pytest.mark.parametrize('account', ['unknown', 'disabled', 'live'])
def test_allowlist_and_live_guard(hub, sessions, account):
    with sessions.begin() as db:
        db.add(Account(id='disabled', platform='facebook', remote_id='disabled', name='disabled', account_type='page', allowlisted=False, is_mock=True))
        db.add(Account(id='live', platform='facebook', remote_id='live', name='live', account_type='page', allowlisted=True, is_mock=False))
    with pytest.raises(HubError) as error:
        write(hub, account=account)
    assert error.value.code in {'account_denied', 'live_disabled'}


def test_instagram_edit_unsupported_and_no_effect(hub, sessions):
    with pytest.raises(HubError) as error:
        hub.write('update_post', 'mock-instagram', {'post_id': 'x', 'text': 'new'}, 'edit', 'tester', SCOPES)
    assert error.value.code == 'unsupported_operation'
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Post)) == 0
        assert db.get(Idempotency, 'edit') is None


def test_facebook_update(hub, sessions):
    result = write(hub)
    hub.write('update_post', 'mock-facebook', {'post_id': f"mock_{result['id']}", 'text': 'Changed'}, 'edit', 'tester', SCOPES)
    with sessions() as db:
        assert db.get(Post, result['id']).content['text'] == 'Changed'


def test_mock_reply_ownership(hub):
    with pytest.raises(HubError) as error:
        hub.write('reply_to_comment', 'mock-facebook', {'comment_id': 'foreign', 'text': 'Hi'}, 'reply', 'tester', SCOPES)
    assert error.value.code == 'comment_not_found'


def test_schedule_persisted_utc(hub, sessions):
    when = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    payload = {'text': 'Later', 'run_at': when, 'timezone': 'UTC'}
    result = hub.write('schedule_post', 'mock-instagram', payload, 'schedule', 'tester', SCOPES)
    assert result == hub.write('schedule_post', 'mock-instagram', payload, 'schedule', 'tester', SCOPES)
    with sessions() as db:
        job = db.get(Job, result['id'])
        assert job.state == 'pending_mock' and job.authorization_subject == 'tester'
        assert job.run_at.tzinfo is None


@pytest.mark.parametrize('when,zone', [('2020-01-01T00:00:00+00:00', 'UTC'), ('2030-01-01T10:00:00', 'Europe/Amsterdam'), ('2030-07-01T10:00:00+01:00', 'Europe/Amsterdam'), ('2030-01-01T00:00:00+00:00', 'Invalid/Zone'), ('invalid', 'UTC')])
def test_invalid_schedule(hub, when, zone):
    with pytest.raises(HubError) as error:
        hub.schedule_time(when, zone)
    assert error.value.code == 'invalid_schedule'


def test_dst_fall_back_disambiguated(hub):
    a = hub.schedule_time('2030-10-27T02:30:00+02:00', 'Europe/Amsterdam')
    b = hub.schedule_time('2030-10-27T02:30:00+01:00', 'Europe/Amsterdam')
    assert b - a == timedelta(hours=1)
