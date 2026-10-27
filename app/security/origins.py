"""Exact origins; forwarding headers have authority only at the configured proxy boundary."""
import ipaddress
import logging
from urllib.parse import urlsplit
from starlette.responses import PlainTextResponse


def browser_request(request, settings):
    if request.headers.get('host') != urlsplit(settings.browser_origin).netloc:
        return False
    if request.url.scheme == 'https':
        return request.headers.get('x-forwarded-proto', 'https') == 'https'
    try:
        peer = ipaddress.ip_address(request.client.host)
        trusted = any(peer in ipaddress.ip_network(c) for c in settings.trusted_proxy_cidrs)
    except (ValueError, AttributeError):
        trusted = False
    return trusted and request.headers.get('x-forwarded-proto') == 'https'


class OriginBoundary:
    def __init__(self, app, settings):
        self.app = app
        self.hosts = {urlsplit(settings.public_url).netloc, urlsplit(settings.browser_origin).netloc,
                      'localhost', '127.0.0.1', '127.0.0.1:8000', 'socialhub:8000'}
        self.origins = {settings.browser_origin, settings.public_url.rsplit('/mcp', 1)[0]}

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            headers = scope.get('headers', [])
            hosts = [v.decode() for k,v in headers if k.lower() == b'host']
            origins = [v.decode() for k,v in headers if k.lower() == b'origin']
            if len(hosts) != 1 or hosts[0] not in self.hosts:
                return await PlainTextResponse('Untrusted host', status_code=421)(scope, receive, send)
            if len(origins) > 1 or (origins and origins[0] not in self.origins):
                reason = ('multiple_origins' if len(origins) > 1 else
                          'null_origin' if origins[0] == 'null' else 'unexpected_origin')
                logging.getLogger('socialhub').warning('origin_boundary_denied', extra={'safe_fields': {
                    'reason': reason, 'surface': 'admin' if scope.get('path', '').startswith('/admin/') else 'other'}})
                return await PlainTextResponse('Untrusted origin', status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)
