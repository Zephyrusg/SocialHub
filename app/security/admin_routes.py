"""Server-rendered setup and restrictive feature preferences; no publication consent."""
import asyncio
from html import escape
from urllib.parse import parse_qs
import logging
from starlette.responses import HTMLResponse, RedirectResponse, Response
from app.core import HubError
from app.database.models import Audit
from app.security.origins import browser_request
from app.services.administration import Administration, AdminSessions, AdminOIDC
from app.services.meta_oauth import MetaOAuth
from app.services.feature_flags import FeatureFlags, FEATURES
from app.admin_ui import ASSETS, SECTIONS, layout, render, notice

COOKIE = '__Secure-socialhub-admin'
FLOW_COOKIE = '__Secure-socialhub-admin-oidc'
HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'strict-origin',
    'Content-Security-Policy': "default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
    'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY'}


def page(body, status=200, *, section='overview', title=None, csrf=None):
    return HTMLResponse(layout(body, section=section, title=title, csrf=csrf), status_code=status, headers=HEADERS)


def register_admin_routes(mcp, settings, sessions):
    browser = AdminSessions(settings, sessions)
    oidc = AdminOIDC(settings, sessions)
    setup = Administration(settings, sessions)
    flags = FeatureFlags(settings, sessions)

    def boundary(request):
        browser.enabled()
        if not browser_request(request, settings):
            raise HubError('admin_denied', 'Trusted HTTPS administrator origin required')

    async def owner(request):
        boundary(request)
        return await asyncio.to_thread(browser.authenticate, request.cookies.get(COOKIE))

    def error(exc):
        code = exc.code if isinstance(exc, HubError) else 'admin_unavailable'
        messages = {
            'admin_disabled': (404, 'Administration is disabled.'),
            'portal_denied': (403, 'Administrator identity and recent MFA are required.'),
            'admin_denied': (403, 'Administrator login and exact form verification are required.'),
            'invalid_setup': (400, 'Check the Facebook App ID and App Secret format.'),
            'configuration_conflict': (409, 'Setup changed. Reload the page and review before saving again.'),
            'meta_not_connected': (409, 'Save Meta settings, then connect and verify both accounts first.'),
            'invalid_feature': (400, 'Unsupported feature or invalid setting.'),
            'feature_unavailable': (403, 'Deployment restrictions prevent enabling this feature.'),
            'feature_confirmation_required': (400, 'Review the publishing warning before enabling.'),
        }
        status, message = messages.get(code, (503, 'Setup could not complete. Check local readiness and start a fresh login if needed.'))
        if status == 503:logging.getLogger('socialhub').warning('admin_operation_failed')
        needs_login = code in {'portal_denied', 'admin_denied'}
        if needs_login:
            action = '<a class="button primary" href="/admin/oidc/login">Sign in with your existing account</a>'
        else:
            target = '/admin/connections' if code in {'invalid_setup', 'meta_not_connected'} else '/admin/features' if code in {'invalid_feature', 'configuration_conflict', 'feature_confirmation_required', 'feature_unavailable'} else '/admin/'
            action = '<a class="button primary" href="' + target + '">Reload and review settings</a>'
        return page('<section class="card action-panel">' + notice(message, 'error') + action + '<a class="button secondary" href="/admin/">Return to dashboard</a></section>', status, title='Administrator access' if needs_login else 'Action could not complete')

    async def form(request, fields):
        subject = await owner(request)
        if (request.headers.get('origin') != settings.browser_origin
                or request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded'):
            raise HubError('admin_denied', 'Same-origin administrator form required')
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8192:raise HubError('admin_denied', 'Administrator form too large')
        try:
            values = parse_qs(data.decode('utf-8'), strict_parsing=True, keep_blank_values=True, max_num_fields=5)
            if set(values) != fields or any(len(v) != 1 for v in values.values()):raise ValueError()
            values = {k: v[0] for k, v in values.items()}
            browser.check_csrf(request.cookies.get(COOKIE), values['csrf'])
            if 'revision' in values:
                if not values['revision'].isascii() or not values['revision'].isdigit() or len(values['revision']) > 10:raise ValueError()
                values['revision'] = int(values['revision'])
        except (ValueError, UnicodeError):
            raise HubError('admin_denied', 'Invalid administrator form') from None
        return subject, values

    @mcp.custom_route('/admin', methods=['GET'])
    async def root(request):
        try:
            boundary(request)
            return RedirectResponse('/admin/', status_code=303, headers=HEADERS)
        except HubError as exc:return error(exc)

    @mcp.custom_route('/admin/oidc/login', methods=['GET'])
    async def login(request):
        try:
            boundary(request)
            url, cookie = await oidc.start()
            response = RedirectResponse(url, status_code=303, headers=HEADERS)
            response.set_cookie(FLOW_COOKIE, cookie, max_age=300, path='/admin/oidc', secure=True, httponly=True, samesite='lax')
            return response
        except Exception as exc:return error(exc)

    @mcp.custom_route('/admin/oidc/callback', methods=['GET'])
    async def callback(request):
        try:
            boundary(request)
            session = await oidc.complete(request.query_params, request.cookies.get(FLOW_COOKIE))
            response = RedirectResponse('/admin/', status_code=303, headers=HEADERS)
            response.set_cookie(COOKIE, session, max_age=900, path='/admin', secure=True, httponly=True, samesite='lax')
        except Exception as exc:response = error(exc)
        response.delete_cookie(FLOW_COOKIE, path='/admin/oidc', secure=True, httponly=True, samesite='lax')
        return response

    async def view(request, section):
        try:
            subject = await owner(request)
            if section not in SECTIONS:
                return page(notice('This administration page was not found.', 'error'), 404, title='Page not found')
            status = await asyncio.to_thread(setup.status, subject)
            feature_status = await asyncio.to_thread(flags.status, subject)
            csrf = browser.csrf(request.cookies[COOKIE])
            query = request.query_params.get('q', '')[:120].strip()
            group = request.query_params.get('category', '')[:40]
            state = request.query_params.get('state', '')
            if state not in {'enabled', 'disabled', 'unavailable'}: state = ''
            html = render(section, status, feature_status, csrf, query=query,
                          selected_category=group, selected_state=state,
                          saved=request.query_params.get('saved', ''))
            return HTMLResponse(html, headers=HEADERS)
        except Exception as exc:return error(exc)

    @mcp.custom_route('/admin/', methods=['GET'])
    async def dashboard(request):
        return await view(request, 'overview')

    @mcp.custom_route('/admin/{section}', methods=['GET'])
    async def section_page(request):
        return await view(request, request.path_params['section'])

    @mcp.custom_route('/admin/assets/{asset}', methods=['GET'])
    async def asset(request):
        try:
            boundary(request)
            name = request.path_params['asset']
            if name not in ASSETS:
                return Response(status_code=404, headers=HEADERS)
            return Response(ASSETS[name], media_type='text/css' if name.endswith('.css') else 'text/javascript', headers=HEADERS)
        except Exception as exc:return error(exc)

    @mcp.custom_route('/admin/features', methods=['POST'])
    async def features(request):
        try:
            subject, values = await form(request, {'csrf', 'revision', 'feature', 'enabled', 'confirmation'})
            name = values['feature']
            if name not in FEATURES or name == 'scheduling' or values['enabled'] not in {'true', 'false'} or values['confirmation'] not in {'yes', 'no'}:
                raise HubError('invalid_feature', 'Invalid feature form')
            enabled = values['enabled'] == 'true'
            if enabled and FEATURES[name][2] and values['confirmation'] != 'yes':
                current = await asyncio.to_thread(flags.status, subject)
                if current['revision'] != values['revision']:raise HubError('configuration_conflict', 'Features changed')
                if not next(f for f in current['features'] if f['name'] == name)['available']:raise HubError('feature_unavailable', 'Feature unavailable')
                hidden = ''.join('<input type="hidden" name="' + key + '" value="' + escape(str(value), quote=True) + '">' for key, value in (values | {'confirmation':'yes'}).items())
                return page('<section class="card action-panel"><h2>Enable ' + escape(FEATURES[name][0]) + '?</h2><p>This allows the corresponding write workflow within deployment restrictions. It does not approve any content. Existing authentication, account restrictions, immutable previews and approval policy still apply. ChatGPT direct mode relies on an authenticated chat instruction; the server cannot independently verify user consent.</p><form method="post" action="/admin/features">' + hidden + '<button class="button primary" data-loading-label="Saving…">Confirm enable</button></form><a class="button secondary" href="/admin/features">Cancel</a></section>', section='features', title='Confirm feature change', csrf=values['csrf'])
            await asyncio.to_thread(flags.save, subject, values['revision'], name, enabled, acknowledged=values['confirmation'] == 'yes')
            return RedirectResponse('/admin/features?saved=feature', status_code=303, headers=HEADERS)
        except Exception as exc:return error(exc)

    @mcp.custom_route('/admin/meta/settings', methods=['POST'])
    async def save(request):
        try:
            subject, values = await form(request, {'csrf', 'revision', 'app_id', 'app_secret'})
            await asyncio.to_thread(setup.save, subject, values['revision'], app_id=values['app_id'], app_secret=values['app_secret'] or None)
            return RedirectResponse('/admin/connections?saved=meta', status_code=303, headers=HEADERS)
        except Exception as exc:return error(exc)

    @mcp.custom_route('/admin/meta/connect', methods=['POST'])
    async def connect(request):
        try:
            subject, values = await form(request, {'csrf', 'revision'})
            def issue():
                oauth = MetaOAuth(settings, sessions)
                if oauth.settings._meta_configuration_revision != values['revision']:
                    raise HubError('configuration_conflict', 'Setup changed')
                link = oauth.issue_login()
                with sessions.begin() as db:db.add(Audit(subject=subject, operation='admin_meta_login_issued', outcome='success', details={'revision': values['revision']}))
                return link
            link = await asyncio.to_thread(issue)
            # Start cross-origin OAuth through a fresh GET navigation rather than
            # a form redirect, which would conflict with form-action 'self'.
            return page('<section class="card action-panel"><h2>Connect your Meta accounts</h2><p>Continue to Facebook to select and verify the allowlisted accounts.</p><a class="button primary" href="' + escape(link, quote=True) + '">Continue to Facebook</a><a class="button secondary" href="/admin/connections">Cancel</a></section>', section='connections', title='Connect Meta')
        except Exception as exc:return error(exc)

    @mcp.custom_route('/admin/meta/reads', methods=['POST'])
    async def reads(request):
        try:
            subject, values = await form(request, {'csrf', 'revision', 'enabled'})
            if values['enabled'] not in {'true', 'false'}:raise HubError('admin_denied', 'Invalid read setting')
            await asyncio.to_thread(setup.save, subject, values['revision'], live_reads=values['enabled'] == 'true')
            return RedirectResponse('/admin/connections?saved=reads', status_code=303, headers=HEADERS)
        except Exception as exc:return error(exc)

    @mcp.custom_route('/admin/logout', methods=['POST'])
    async def logout(request):
        try:
            _, values = await form(request, {'csrf'})
            await asyncio.to_thread(browser.logout, request.cookies.get(COOKIE))
            response = page('<section class="card action-panel"><h2>Signed out</h2><p>Signed out of SocialHub setup.</p><a class="button primary" href="/admin/oidc/login">Sign in again</a></section>', title='Signed out')
            response.delete_cookie(COOKIE, path='/admin', secure=True, httponly=True, samesite='lax')
            return response
        except Exception as exc:return error(exc)
