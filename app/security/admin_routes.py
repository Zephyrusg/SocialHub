"""Server-rendered setup and restrictive feature preferences; no publication consent."""
import asyncio
from html import escape
from urllib.parse import parse_qs
import logging
from starlette.responses import HTMLResponse, RedirectResponse
from app.core import HubError
from app.database.models import Audit
from app.security.origins import browser_request
from app.services.administration import Administration, AdminSessions, AdminOIDC
from app.services.meta_oauth import MetaOAuth
from app.services.feature_flags import FeatureFlags, FEATURES

COOKIE = '__Secure-socialhub-admin'
FLOW_COOKIE = '__Secure-socialhub-admin-oidc'
HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'strict-origin',
    'Content-Security-Policy': "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY'}


def page(body, status=200):
    return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width"><title>SocialHub setup</title>'
        '<main><h1>SocialHub setup</h1>' + body + '</main></html>', status_code=status, headers=HEADERS)


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
        return page('<p>' + message + '</p><a href="/admin/oidc/login">Sign in with your existing account</a>', status)

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

    @mcp.custom_route('/admin/', methods=['GET'])
    async def dashboard(request):
        try:
            subject = await owner(request)
            status = await asyncio.to_thread(setup.status, subject)
            csrf = escape(browser.csrf(request.cookies[COOKIE]), quote=True)
            hidden = '<input type="hidden" name="csrf" value="' + csrf + '"><input type="hidden" name="revision" value="' + str(status['revision']) + '">'
            roles = '<p>Administrator: yes. Publishing permission for this verified subject: ' + ('yes' if status['publishing_permission'] else 'no') + '.</p>'
            gates = '<p>Live writes: ' + ('enabled by operator' if status['live_writes'] else 'disabled') + '. Approval policy: ' + escape(status['approval_mode']) + '. Each write still needs its required approval.</p>'
            accounts = '<ul>' + ''.join('<li>' + escape(a['account_id']) + ': ' + ('connected' if a['connected'] else 'not connected or expired') + '</li>' for a in status['accounts']) + '</ul>'
            body = roles + gates + '<h2>Meta connection</h2>' + accounts
            body += '<p>Use the main Facebook App credentials. Secret saved: ' + ('yes' if status['meta_secret_configured'] else 'no') + '. Secret values are never displayed.</p>'
            body += '<form method="post" action="/admin/meta/settings">' + hidden + '<label>Facebook App ID <input required name="app_id" maxlength="64" value="' + escape(status['meta_app_id'], quote=True) + '"></label><label>App Secret <input type="password" name="app_secret" maxlength="256" autocomplete="new-password"></label><p>Leave the secret blank to keep the current value. Changing credentials disables reads and requires a fresh account connection.</p><button>Save Meta settings</button></form>'
            body += '<form method="post" action="/admin/meta/connect">' + hidden + '<button>Connect Facebook and Instagram</button></form>'
            body += '<p>Live account reads: ' + ('enabled' if status['live_reads'] else 'disabled') + '.</p><form method="post" action="/admin/meta/reads">' + hidden + '<input type="hidden" name="enabled" value="' + ('false' if status['live_reads'] else 'true') + '"><button>' + ('Disable' if status['live_reads'] else 'Enable verified') + ' account reads</button></form>'
            feature_status = await asyncio.to_thread(flags.status, subject)
            body += '<h2>Feature Flags</h2><p>Configured preferences persist immediately. Deployment restrictions are hard limits; enabling a feature never approves a post. Disabling stops new execution, but cannot recall requests already sent to Meta. Read-only reconciliation remains available.</p>'
            body += '<table><thead><tr><th scope="col">Feature</th><th scope="col">Configured</th><th scope="col">Available</th><th scope="col">Effective</th><th scope="col">Control</th></tr></thead><tbody>'
            for feature in feature_status['features']:
                configured = 'Inherit deployment' if feature['configured'] is None else ('On' if feature['configured'] else 'Off')
                on = feature['effective']
                # Allow an explicitly paused preference to be resumed even when
                # another runtime dependency is off; effective state stays off.
                requested = feature['configured'] if feature['configured'] is not None else True
                desired = not requested
                body += '<tr><th scope="row">' + escape(feature['label']) + '<p>' + escape(feature['description']) + '</p></th><td>' + configured + '</td><td>' + ('Yes' if feature['available'] else 'No') + '</td><td>' + ('On' if on else 'Off') + '<p>' + escape(feature['reason']) + '</p></td><td>'
                if feature['available']:
                    body += '<form method="post" action="/admin/features"><input type="hidden" name="csrf" value="' + csrf + '"><input type="hidden" name="revision" value="' + str(feature_status['revision']) + '"><input type="hidden" name="feature" value="' + feature['name'] + '"><input type="hidden" name="enabled" value="' + ('true' if desired else 'false') + '"><input type="hidden" name="confirmation" value="no"><button role="switch" aria-checked="' + ('true' if requested else 'false') + '" aria-label="' + escape(feature['label'], quote=True) + '">' + ('Enable' if desired else 'Disable') + '</button></form>'
                else:body += '<button disabled aria-label="' + escape(feature['label'], quote=True) + '">Unavailable</button>'
                body += '</td></tr>'
            body += '</tbody></table><p>Identity roles, account allowlists, encryption keys, public origins and deployment ceilings remain protected operator configuration.</p><form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="' + csrf + '"><button>Sign out</button></form>'
            return page(body)
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
                return page('<h2>Enable ' + escape(FEATURES[name][0]) + '?</h2><p>This allows the corresponding write workflow within deployment restrictions. It does not approve any content. Existing authentication, account restrictions, immutable previews and approval policy still apply. ChatGPT direct mode relies on an authenticated chat instruction; the server cannot independently verify user consent.</p><form method="post" action="/admin/features">' + hidden + '<button>Confirm enable</button></form><a href="/admin/">Cancel</a>')
            await asyncio.to_thread(flags.save, subject, values['revision'], name, enabled, acknowledged=values['confirmation'] == 'yes')
            return RedirectResponse('/admin/', status_code=303, headers=HEADERS)
        except Exception as exc:return error(exc)

    @mcp.custom_route('/admin/meta/settings', methods=['POST'])
    async def save(request):
        try:
            subject, values = await form(request, {'csrf', 'revision', 'app_id', 'app_secret'})
            await asyncio.to_thread(setup.save, subject, values['revision'], app_id=values['app_id'], app_secret=values['app_secret'] or None)
            return RedirectResponse('/admin/', status_code=303, headers=HEADERS)
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
            return page('<p>Continue to Facebook to select and verify the allowlisted accounts.</p><a href="' + escape(link, quote=True) + '">Continue to Facebook</a>')
        except Exception as exc:return error(exc)

    @mcp.custom_route('/admin/meta/reads', methods=['POST'])
    async def reads(request):
        try:
            subject, values = await form(request, {'csrf', 'revision', 'enabled'})
            if values['enabled'] not in {'true', 'false'}:raise HubError('admin_denied', 'Invalid read setting')
            await asyncio.to_thread(setup.save, subject, values['revision'], live_reads=values['enabled'] == 'true')
            return RedirectResponse('/admin/', status_code=303, headers=HEADERS)
        except Exception as exc:return error(exc)

    @mcp.custom_route('/admin/logout', methods=['POST'])
    async def logout(request):
        try:
            _, values = await form(request, {'csrf'})
            await asyncio.to_thread(browser.logout, request.cookies.get(COOKIE))
            response = page('<p>Signed out of SocialHub setup.</p>')
            response.delete_cookie(COOKIE, path='/admin', secure=True, httponly=True, samesite='lax')
            return response
        except Exception as exc:return error(exc)
