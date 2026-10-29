"""Trusted local reconciliation evidence; never uploads, publishes or guesses an ID."""
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlsplit

from app.core import HubError


def reel_timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:raise ValueError()
        return stamp.astimezone(timezone.utc).replace(tzinfo=None)
    except (AttributeError, TypeError, ValueError):
        return None


async def verified_instagram_media(adapter, account_id, request, checkpoint, remote_id, started, attempted):
    """Correlate one owner-selected Instagram media with a published saved container.

    Container status alone exposes no media ID. Require a unique account-owned
    Instagram media, exact caption and a bounded execution window before accepting the ID.
    This is explicit local recovery, not automatic matching in a status poll.
    """
    photo = request['content_type'] == 'image'
    ready_stage = 'ig_photo_ready' if photo else 'ig_reel_ready'
    container = checkpoint.get('container_id')
    if (adapter.platform != 'instagram' or adapter.graph.allow_writes
            or checkpoint.get('stage') != ready_stage
            or checkpoint.get('remote_id') != container
            or not isinstance(container, str) or not re.fullmatch('[0-9]{1,64}', container)
            or not isinstance(remote_id, str) or not re.fullmatch('[0-9]{1,64}', remote_id)
            or attempted is None or attempted < started):
        raise HubError('reconciliation_denied', 'A saved Instagram finish attempt and read-only connector are required')
    adapter.connector.check_account(account_id)
    status = await adapter.read(container, {'fields': 'id,status_code'})
    if status.get('id') != container or status.get('status_code') != 'PUBLISHED':
        raise HubError('reconciliation_denied', 'The saved Instagram container is not confirmed PUBLISHED; never resubmit media_publish')
    candidates = {}
    cursor, seen = None, set()
    for _ in range(10):
        params = {'fields': 'id,caption,media_type,media_product_type,permalink,timestamp', 'limit': 100}
        if cursor:params['after'] = cursor
        page = adapter.connector.page(await adapter.read(account_id + '/media', params))
        for post in page['items']:
            stamp = reel_timestamp(post.get('timestamp'))
            if (post.get('media_type') == ('IMAGE' if photo else 'VIDEO') and post.get('media_product_type') == ('FEED' if photo else 'REELS')
                    and (post.get('caption') or '') == request['text'] and stamp is not None
                    and started - timedelta(minutes=2) <= stamp <= attempted + timedelta(minutes=15)):
                pid = post.get('id')
                if not isinstance(pid, str) or not re.fullmatch('[0-9]{1,64}', pid):
                    raise HubError('reconciliation_denied', 'Instagram returned an invalid media identifier')
                candidates[pid] = post
        cursor = page['next_cursor']
        if not cursor:break
        if cursor in seen:
            raise HubError('reconciliation_denied', 'Instagram ownership listing repeated a cursor')
        seen.add(cursor)
    else:
        raise HubError('manual_check_required', 'The bounded account listing was incomplete; no outcome was accepted')
    if len(candidates) != 1 or remote_id not in candidates:
        raise HubError('reconciliation_denied', 'Require one unique account-owned Instagram media matching the exact caption and execution window')
    post = candidates[remote_id]
    try:
        url = urlsplit(post.get('permalink', ''))
        if (url.scheme != 'https' or url.hostname not in {'instagram.com', 'www.instagram.com'}
                or url.username or url.password or url.port not in {None, 443}
                or not re.fullmatch(('/p/' if photo else '/reel/') + '[A-Za-z0-9_-]+/?', url.path) or url.query or url.fragment):
            raise ValueError()
    except (TypeError, ValueError):
        raise HubError('reconciliation_denied', 'A verified Instagram media permalink is required') from None
    return post
