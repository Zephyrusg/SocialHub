"""Every response is fixture data; no Meta connection is made."""
from app.connectors.meta import FacebookConnector, InstagramConnector
from app.core import HubError


class MockConnector:
    def __init__(self, platform):
        self.platform = platform

    def capabilities(self, account_type):
        connector = FacebookConnector if self.platform == 'facebook' else InstagramConnector
        return connector.capabilities(self, account_type)

    async def list_posts(self, remote_id, limit=25, cursor=None):
        return {'items': [{'id': f'{remote_id}_sample', 'text': 'PixelPapa mock post', 'mock': True}][:limit] if not cursor else [], 'next_cursor': None}

    async def get_post(self, remote_id, post_id):
        if post_id != f'{remote_id}_sample':
            raise HubError('post_not_found', 'Mock post not found for this account')
        return {'id': post_id, 'text': 'PixelPapa mock post', 'mock': True}

    async def list_comments(self, remote_id, post_id, limit=25, cursor=None):
        await self.get_post(remote_id, post_id)
        return {'items': [{'id': f'{post_id}_comment', 'text': 'Mock comment', 'mock': True}][:limit] if not cursor else [], 'next_cursor': None}
