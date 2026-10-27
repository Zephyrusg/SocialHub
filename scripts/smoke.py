"""Authenticated real MCP client smoke test, including nginx SSE framing."""
import asyncio
import os
from uuid import uuid4
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from app.config import Settings


async def main():
    s = Settings()
    url = os.environ.get('SMOKE_URL', s.public_url)
    headers = {'Authorization': 'Bearer ' + s.local_token.get_secret_value()}
    async with httpx.AsyncClient(timeout=10) as client:
        assert (await client.get(url.replace('/mcp', '/readyz'))).status_code == 200
        response = await client.post(url, json={})
        assert response.status_code == 401 and 'resource_metadata' in response.headers['www-authenticate']
        init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'smoke', 'version': '1'}}}
        response = await client.post(url, json=init, headers=headers | {'Accept': 'application/json, text/event-stream'})
        assert response.status_code == 200, response.text
        assert response.headers['content-type'].startswith('text/event-stream')
        assert 'data: ' in response.text
        if ':8787/' in url:
            assert response.headers['x-accel-buffering'] == 'no'
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert {'list_accounts', 'list_posts', 'get_post', 'list_comments', 'publish_post', 'update_post', 'reply_to_comment', 'schedule_post'} <= {t.name for t in tools.tools}
            if s.meta_live_reads:
                result = await client.call_tool('list_accounts', {})
                accounts = result.structuredContent['data']['accounts']
                if not accounts:
                    raise RuntimeError('Complete local Meta Facebook Login before the live MCP smoke test')
                assert {a['id'] for a in accounts} == {'pixelpapa-facebook', 'pixelpapa-instagram'}
                for account in accounts:
                    aid = account['id']
                    caps = await client.call_tool('get_account_capabilities', {'account_id': aid})
                    assert caps.structuredContent['ok'] and not caps.structuredContent['data']['capabilities']['live_writes_enabled']
                    posts = await client.call_tool('list_posts', {'account_id': aid, 'limit': 25})
                    assert posts.structuredContent['ok']
                    items = posts.structuredContent['data']['items']
                    if items:
                        args = {'account_id': aid, 'post_id': items[0]['id']}
                        assert (await client.call_tool('get_post', args)).structuredContent['ok']
                        if caps.structuredContent['data']['capabilities']['list_comments']:
                            assert (await client.call_tool('list_comments', args)).structuredContent['ok']
                print(f'PASS: {url}: live read-only MCP/SSE, tool discovery, authorized PixelPapa accounts, post reads and capabilities; no write tools called')
                return
            result = await client.call_tool('list_accounts', {})
            assert len(result.structuredContent['data']['accounts']) == 2
            for aid in ['mock-facebook', 'mock-instagram']:
                caps = await client.call_tool('get_account_capabilities', {'account_id': aid})
                assert caps.structuredContent['data']['mock']
                posts = await client.call_tool('list_posts', {'account_id': aid})
                post_id = posts.structuredContent['data']['items'][0]['id']
                assert (await client.call_tool('get_post', {'account_id': aid, 'post_id': post_id})).structuredContent['ok']
                assert (await client.call_tool('list_comments', {'account_id': aid, 'post_id': post_id})).structuredContent['ok']
            invalid = await client.call_tool('list_posts', {'account_id': 'mock-facebook', 'limit': 0})
            assert invalid.isError
            args = {'account_id': 'mock-facebook', 'text': 'Phase 1 mock-only smoke test', 'idempotency_key': 'smoke-' + uuid4().hex}
            first = await client.call_tool('publish_post', args)
            second = await client.call_tool('publish_post', args)
            assert first.structuredContent['data']['id'] == second.structuredContent['data']['id']
            assert first.structuredContent['data']['mock']
            unsupported = await client.call_tool('update_post', {'account_id': 'mock-instagram', 'post_id': 'x', 'text': 'Hi', 'idempotency_key': 'unsupported-' + uuid4().hex})
            assert unsupported.structuredContent['error']['code'] == 'unsupported_operation'
            status = await client.call_tool('get_system_status', {})
            assert status.structuredContent['data']['database'] == 'ok'
    print(f'PASS: {url}: real SDK initialization, tool discovery, read tools, validation, auth, SSE, mock write deduplication, unsupported edit, MariaDB health')


if __name__ == '__main__':
    asyncio.run(main())
