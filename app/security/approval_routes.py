"""Independent HTTPS owner portal: server-rendered forms, no browser JS dependencies."""
import asyncio
from html import escape
import json
from urllib.parse import parse_qs
from sqlalchemy import select
from starlette.responses import HTMLResponse, RedirectResponse, FileResponse
from app.core import HubError
from app.security.origins import browser_request
from app.services.owner_oidc import OwnerOIDC
import httpx
import jwt
from app.database.models import Publication
from app.services.approval_portal import OwnerPortal
from app.services.publishing import PublishingService

COOKIE = '__Secure-socialhub-owner'
FLOW_COOKIE = '__Secure-socialhub-oidc'
HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'strict-origin',
    'Content-Security-Policy': "default-src 'none'; img-src 'self'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY'}


def page(content, status=200):
    return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                        '<title>SocialHub owner approvals</title><main><h1>SocialHub owner approvals</h1>' + content + '</main></html>',
                        status_code=status, headers=HEADERS)


def register_approval_routes(mcp, settings, sessions):
    portal = OwnerPortal(settings, sessions)
    oidc = OwnerOIDC(settings, sessions)
    publishing = PublishingService(settings, sessions)

    def owner(request):
        portal.enabled()
        # The configured trusted TLS proxy overwrites the forwarding scheme.
        if not browser_request(request, settings):
            raise HubError('portal_denied', 'Use the configured HTTPS owner portal')
        return portal.authenticate(request.cookies.get(COOKIE))

    def denied(error):
        return page('<p>' + escape(error.code) + ': Owner access or exact form verification is required.</p>',
                    404 if error.code == 'portal_disabled' else 403)

    async def form(request):
        owner(request)
        if request.headers.get('origin') != settings.browser_origin or request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise HubError('portal_denied', 'Same-origin owner form required')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                raise HubError('portal_denied', 'Owner form too large')
        try:
            values = parse_qs(body.decode('utf-8'), strict_parsing=True, max_num_fields=4)
        except (ValueError, UnicodeError):
            raise HubError('portal_denied', 'Invalid owner form') from None
        if any(len(v) != 1 for v in values.values()):
            raise HubError('portal_denied', 'Duplicate form fields refused')
        values = {k: v[0] for k, v in values.items()}
        portal.check_csrf(request.cookies.get(COOKIE), values.get('csrf'))
        return values

    @mcp.custom_route('/approval/oidc/login', methods=['GET'])
    async def oidc_login(request):
        try:
            if not browser_request(request, settings):
                raise HubError('portal_denied', 'Trusted HTTPS browser origin required')
            url, browser = await oidc.start()
            response = RedirectResponse(url, status_code=303, headers=HEADERS)
            response.set_cookie(FLOW_COOKIE, browser, max_age=300, path='/approval/oidc', secure=True, httponly=True, samesite='lax')
            return response
        except (HubError, httpx.HTTPError, ValueError, KeyError):
            return denied(HubError('portal_denied', 'Owner OIDC login unavailable'))

    @mcp.custom_route('/approval/oidc/callback', methods=['GET'])
    async def oidc_callback(request):
        try:
            if not browser_request(request, settings):
                raise HubError('portal_denied', 'Trusted HTTPS browser origin required')
            browser = await oidc.complete(request.query_params, request.cookies.get(FLOW_COOKIE))
            response = RedirectResponse('/approval/', status_code=303, headers=HEADERS)
            response.set_cookie(COOKIE, browser, max_age=900, path='/approval', secure=True, httponly=True, samesite='lax')
        except (HubError, httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, TypeError):
            response = denied(HubError('portal_denied', 'Owner OIDC verification failed'))
        response.delete_cookie(FLOW_COOKIE, path='/approval/oidc', secure=True, httponly=True, samesite='lax')
        return response

    @mcp.custom_route('/approval/login', methods=['GET'])
    async def login(request):
        try:
            portal.enabled()
            if (not browser_request(request, settings)
                    or len(request.query_params.getlist('ticket')) != 1):
                raise HubError('portal_denied', 'One trusted HTTPS owner login required')
            browser = await asyncio.to_thread(portal.start, request.query_params.get('ticket'))
            response = RedirectResponse('/approval/', status_code=303, headers=HEADERS)
            response.set_cookie(COOKIE, browser, max_age=900, path='/approval', secure=True, httponly=True, samesite='lax')
            return response
        except HubError as error:
            return denied(error)

    @mcp.custom_route('/approval/', methods=['GET'])
    async def index(request):
        try:
            owner(request)
            with sessions() as db:
                rows = list(db.scalars(select(Publication).order_by(Publication.created_at.desc()).limit(50)))
                links = ''.join('<li><a href="/approval/publications/' + escape(p.id, quote=True) + '">' + escape(p.id) + '</a></li>' for p in rows)
            csrf = escape(portal.csrf(request.cookies[COOKIE]), quote=True)
            return page('<p>Optional independent owner channel. ChatGPT remains the preferred interface.</p><ul>' + links +
                '</ul><form method="post" action="/approval/logout"><input type="hidden" name="csrf" value="' + csrf + '"><button>Sign out</button></form>')
        except HubError as error:
            return denied(error)

    @mcp.custom_route('/approval/publications/{publication_id}', methods=['GET'])
    async def review(request):
        try:
            owner(request)
            preview = publishing.review(request.path_params['publication_id'], None)
            csrf = escape(portal.csrf(request.cookies[COOKIE]), quote=True)
            action = '/approval/publications/' + escape(preview['publication_id'], quote=True)
            images = ''.join('<img alt="Exact reviewed image" width="320" src="' + action + '/media/' + escape(a['id'], quote=True) + '">'
                for a in preview['preview']['assets'] if a['mime_type'].startswith('image/'))
            hidden = '<input type="hidden" name="csrf" value="' + csrf + '"><input type="hidden" name="request_hash" value="' + preview['request_hash'] + '">'
            # No form executes a platform action. Full exact request is escaped, never rendered as markup.
            return page('<p>Review the exact content, accounts and hash. Approval does not publish.</p>' + images +
                '<pre>' + escape(json.dumps(preview, indent=2, ensure_ascii=False)) + '</pre>' +
                '<form method="post" action="' + action + '/approve">' + hidden +
                '<label>Explicit instruction <textarea required maxlength="4096" name="instruction"></textarea></label><button>Approve exact request</button></form>' +
                '<form method="post" action="' + action + '/revoke">' + hidden + '<button>Revoke or deny future steps</button></form>')
        except HubError as error:
            return denied(error)

    @mcp.custom_route('/approval/publications/{publication_id}/media/{media_id}', methods=['GET', 'HEAD'])
    async def media(request):
        try:
            owner(request)
            preview = publishing.review(request.path_params['publication_id'], None)
            asset = next((a for a in preview['preview']['assets'] if a['id'] == request.path_params['media_id']), None)
            if not asset:
                raise HubError('media_access_denied', 'Asset is not in this exact preview')
            path, mime = await asyncio.to_thread(publishing.media.path, asset['id'], asset['sha256'])
            return FileResponse(path, media_type=mime, headers=HEADERS)
        except HubError as error:
            return denied(error)

    @mcp.custom_route('/approval/publications/{publication_id}/approve', methods=['POST'])
    async def approve(request):
        try:
            issuer = owner(request)
            values = await form(request)
            if set(values) != {'csrf', 'request_hash', 'instruction'} or not 1 <= len(values['instruction'].strip()) <= 4096:
                raise HubError('portal_denied', 'An exact owner instruction is required')
            await asyncio.to_thread(publishing.approve, request.path_params['publication_id'], values['request_hash'],
                                    issuer, values['instruction'], channel='owner_portal')
            return page('<p>Exact owner approval recorded. No content was published. Return to ChatGPT to execute this publication.</p>')
        except HubError as error:
            return denied(error)

    @mcp.custom_route('/approval/publications/{publication_id}/revoke', methods=['POST'])
    async def revoke(request):
        try:
            issuer = owner(request)
            values = await form(request)
            if set(values) != {'csrf', 'request_hash'}:
                raise HubError('portal_denied', 'Exact owner form required')
            await asyncio.to_thread(publishing.revoke, request.path_params['publication_id'], values['request_hash'], issuer)
            return page('<p>Future steps denied. Already dispatched operations cannot be undone by revocation.</p>')
        except HubError as error:
            return denied(error)

    @mcp.custom_route('/approval/logout', methods=['POST'])
    async def logout(request):
        try:
            await form(request)
            await asyncio.to_thread(portal.logout, request.cookies[COOKIE])
            response = page('<p>Signed out.</p>')
            response.delete_cookie(COOKIE, path='/approval', secure=True, httponly=True, samesite='lax')
            return response
        except HubError as error:
            return denied(error)
