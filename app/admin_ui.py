"""Small escaped presentation components for the administrator's existing workflows."""
from dataclasses import dataclass
from hashlib import sha256
from html import escape
from pathlib import Path

ASSETS = {name: (Path(__file__).parent / 'static' / name).read_bytes() for name in ('admin.css', 'admin.js')}
VERSIONS = {name: sha256(data).hexdigest()[:12] for name, data in ASSETS.items()}
SECTIONS = {'overview': ('Overview', '/admin/', 'Your connections and publishing controls, at a glance.'),
    'connections': ('Connections', '/admin/connections', 'Connect your accounts. Keep credentials protected.'),
    'features': ('Feature Flags', '/admin/features', 'Manage platform capabilities without restarting SocialHub.'),
    'security': ('Security', '/admin/security', 'Understand who has access and how publishing is authorized.'),
    'settings': ('General Settings', '/admin/settings', 'Application information and configuration ownership.')}
ICONS = {'overview':'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    'connections':'M8 3v5 M16 3v5 M6 8h12v4a6 6 0 0 1-12 0z M12 18v4',
    'features':'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',
    'security':'M12 3 3 7v6c0 5 9 9 9 9s9-4 9-9V7z',
    'settings':'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z',
    'brand':'M8 5a9 9 0 0 0 0 14 M16 5a9 9 0 0 1 0 14 M10 8a5 5 0 0 0 0 8 M14 8a5 5 0 0 1 0 8 M12 11v7 M10 21l2-3 2 3'}


def icon(name):
    return '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="' + ICONS[name] + '"></path></svg>'


def badge(label, tone='neutral'):
    return '<span class="badge ' + tone + '"><span class="status-dot" aria-hidden="true"></span>' + escape(label) + '</span>'


def notice(message, tone='info'):
    return '<div class="notice ' + tone + '" role="' + ('alert' if tone == 'error' else 'status') + '">' + escape(message) + '</div>'


def hidden(csrf, revision=None):
    result = '<input type="hidden" name="csrf" value="' + escape(csrf, quote=True) + '">'
    if revision is not None: result += '<input type="hidden" name="revision" value="' + str(revision) + '">'
    return result


def layout(body, *, section='overview', title=None, connection=None, csrf=None):
    title = title or SECTIONS[section][0]
    nav = ''.join('<a class="nav-link' + (' active' if key == section else '') + '" href="' + path + '"' + (' aria-current="page"' if key == section else '') + '>' + icon(key) + '<span>' + label + '</span></a>' for key, (label, path, _) in SECTIONS.items())
    signout = '<form method="post" action="/admin/logout" class="signout">' + hidden(csrf) + '<button class="button quiet">Sign out</button></form>' if csrf else ''
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="dark"><title>' + escape(title) + ' · SocialHub</title><link rel="stylesheet" href="/admin/assets/admin.css?v=' + VERSIONS['admin.css'] + '"><script defer src="/admin/assets/admin.js?v=' + VERSIONS['admin.js'] + '"></script></head><body><a class="skip-link" href="#main">Skip to content</a><div class="app-shell"><header class="app-header"><a class="brand" href="/admin/" aria-label="SocialHub dashboard"><span class="brand-mark">' + icon('brand') + '</span><span><strong>SocialHub</strong><small>Administration</small></span></a>' + (connection or badge('Secure administration')) + '</header><div class="app-layout"><aside class="sidebar"><nav aria-label="Administration">' + nav + '</nav><div class="sidebar-foot"><span class="eyebrow">Your accounts. Your control.</span>' + signout + '</div></aside><main id="main" tabindex="-1"><div class="page-heading"><div><span class="eyebrow">SocialHub administration</span><h1>' + escape(title) + '</h1><p>' + escape(SECTIONS[section][2]) + '</p></div>' + ('<a class="text-link dashboard-link" href="/admin/">Back to dashboard</a>' if section != 'overview' else '') + '</div>' + body + '</main></div><footer class="app-footer">SocialHub · Private account management<span>Every publication starts with your instruction.</span></footer></div></body></html>'


@dataclass(frozen=True)
class AccountView:
    name: str
    platform: str
    identifier: str
    connected: bool


@dataclass(frozen=True)
class ProviderView:
    name: str
    description: str
    accounts: tuple[AccountView, ...]
    configuration: str = ''
    avatar: bool = False


def provider_card(provider):
    connected = sum(a.connected for a in provider.accounts)
    state = badge('Connected', 'success') if connected == len(provider.accounts) and connected else badge('Partially connected' if connected else 'Not connected', 'warning')
    accounts = ''.join('<li class="account-row"><span class="platform-mark" aria-hidden="true">' + escape(a.platform[:1]) + '</span><span class="account-name"><strong>' + escape(a.name) + '</strong><small>' + escape(a.platform) + '</small></span>' + badge('Connected' if a.connected else 'Needs connection', 'success' if a.connected else 'warning') + '</li>' for a in provider.accounts)
    mark = '<img class="provider-mark" src="/admin/youtube/avatar" alt="Verified channel avatar" width="40" height="40">' if provider.avatar else '<span class="provider-mark" aria-hidden="true">' + escape(provider.name[:1]) + '</span>'
    return '<article class="card provider-card"><div class="card-heading"><div class="provider-title">' + mark + '<div><h2>' + escape(provider.name) + '</h2><p>' + escape(provider.description) + '</p></div></div>' + state + '</div><ul class="account-list">' + accounts + '</ul>' + provider.configuration + '</article>'


def category(name):
    if name.startswith('facebook_'): return 'Facebook'
    if name.startswith('instagram_'): return 'Instagram'
    if name.startswith('youtube_'): return 'YouTube'
    if name in {'media_import', 'media_delivery', 'attachment_probe', 'media_normalization'}: return 'Media'
    if name == 'chatgpt_direct': return 'Publishing & Approval'
    return 'System'


def feature_state(feature):
    return 'unavailable' if not feature['available'] else 'enabled' if feature['effective'] else 'disabled'


def feature_rows(feature_status, csrf, query='', selected_category='', selected_state=''):
    rows = []
    total = 0
    for f in feature_status['features']:
        group = category(f['name']); state = feature_state(f)
        searchable = (f['label'] + ' ' + f['description']).lower()
        matches = (not query or query.lower() in searchable) and (not selected_category or group == selected_category) and (not selected_state or state == selected_state)
        total += int(matches)
        requested = f['configured'] if f['configured'] is not None else f.get('default_enabled', True)
        configured = 'Not implemented' if not f['implemented'] else ('Inherit deployment' if f.get('default_enabled', True) else 'Off by default') if f['configured'] is None else 'On' if requested else 'Off'
        effective = badge('On' if f['effective'] else 'Off', 'success' if f['effective'] else 'neutral')
        if f['available']:
            control = '<form method="post" action="/admin/features" class="toggle-form">' + hidden(csrf, feature_status['revision']) + '<input type="hidden" name="feature" value="' + escape(f['name'], quote=True) + '"><input type="hidden" name="enabled" value="' + ('false' if requested else 'true') + '"><input type="hidden" name="confirmation" value="no"><button class="toggle' + (' is-on' if requested else '') + '" role="switch" aria-checked="' + ('true' if requested else 'false') + '" aria-label="' + escape(f['label'], quote=True) + '" data-loading-label="Saving…"><span class="toggle-track" aria-hidden="true"></span><span>' + ('Disable' if requested else 'Enable') + '</span></button></form>'
        else:
            control = '<button class="toggle unavailable" disabled aria-label="' + escape(f['label'], quote=True) + '"><span class="toggle-track" aria-hidden="true"></span><span>Unavailable</span></button>'
        rows.append('<tr data-feature data-search="' + escape(searchable, quote=True) + '" data-category="' + escape(group, quote=True) + '" data-state="' + state + '"' + ('' if matches else ' hidden') + '><th scope="row" class="feature-name"><strong>' + escape(f['label']) + '</strong><p>' + escape(f['description']) + '</p></th><td data-label="Category"><span class="category-tag">' + escape(group) + '</span></td><td data-label="Configured">' + configured + '</td><td data-label="Availability">' + badge('Available' if f['available'] else 'Not implemented' if not f['implemented'] else 'Restricted', 'info' if f['available'] else 'neutral') + '</td><td data-label="Effective">' + effective + ('<small class="feature-reason">' + escape(f['reason']) + '</small>' if f['reason'] else '') + '</td><td data-label="Control" class="feature-control">' + control + '</td></tr>')
    return ''.join(rows), total


def render(section, status, feature_status, csrf, *, query='', selected_category='', selected_state='', saved=''):
    features = {f['name']: f for f in feature_status['features']}
    accounts = tuple(AccountView('PixelPapa', 'Facebook Page' if a['account_id'].endswith('facebook') else 'Instagram', a['account_id'], a['connected']) for a in status['accounts'])
    meta_accounts = accounts
    yt=status.get('youtube')
    if yt and yt.get('channel_id'):accounts += (AccountView(yt['name'],'YouTube',yt['channel_id'],yt['connected']),)
    connected = sum(a.connected for a in accounts)
    connection = badge('Connected', 'success') if connected == len(accounts) and connected else badge('Partially connected' if connected else 'Not connected', 'warning')
    messages = {'feature': 'Feature preference saved. Effective state is shown below.', 'meta': 'Meta settings saved securely. Credential changes require a new account connection.', 'reads': 'Account-read preference saved.', 'youtube':'YouTube connection settings saved.'}
    body = notice(messages[saved], 'success') if saved in messages else ''
    if section == 'overview':
        cards = [('Service health', 'Online', 'Application and database reachable', 'success'), ('Connected accounts', str(connected) + ' / ' + str(len(accounts)), 'Stored, valid account connections', 'info'), ('Publishing', 'Available' if status['live_writes'] else 'Paused', 'Subject to account, feature and approval checks', 'success' if status['live_writes'] else 'warning')]
        body += '<div class="metric-grid">' + ''.join('<article class="card metric"><span class="eyebrow">' + label + '</span><div class="metric-value ' + tone + '">' + value + '</div><p>' + description + '</p></article>' for label, value, description, tone in cards) + '</div>'
        if connected < len(accounts): body += notice('Some accounts are disconnected or their credentials have expired. Review Connections before using them.', 'warning')
        if not status['live_reads']: body += notice('Live account reads are paused. Stored connection status does not verify current Meta connectivity.', 'warning')
        body += '<div class="section-heading"><h2>Your workspace</h2><span class="muted">A clear path from connection to publication</span></div><div class="two-column"><article class="card"><div class="card-heading"><h2>Connections</h2><a class="text-link" href="/admin/connections">Manage connections →</a></div><div class="provider-summary"><span class="provider-mark">M</span><div><strong>Connected providers</strong><p>Your verified accounts</p></div>' + connection + '</div><ul class="compact-accounts">' + ''.join('<li><span>' + escape(a.platform) + ' · ' + escape(a.name) + '</span>' + badge('Connected' if a.connected else 'Not connected', 'success' if a.connected else 'neutral') + '</li>' for a in accounts) + '</ul><p class="hint">Connection status uses stored credentials; no platform request is made by this dashboard.</p></article><article class="card"><div class="card-heading"><h2>Publishing controls</h2><a class="text-link" href="/admin/features">Manage features →</a></div><ul class="status-list">' + ''.join('<li><span>' + escape(features[n]['label']) + '</span>' + badge('On' if features[n]['effective'] else 'Off', 'success' if features[n]['effective'] else 'neutral') + '</li>' for n in ['facebook_publishing', 'facebook_reels', 'instagram_publishing', 'chatgpt_direct']) + '</ul><p class="hint">Feature activation never publishes content. Prepare, review and explicitly request each publication in ChatGPT.</p></article></div><article class="card identity-summary"><div><h2>Access &amp; authorization</h2><p>Administrator: yes. Publishing permission for this verified subject: ' + ('yes' if status['publishing_permission'] else 'no') + '.</p></div><a class="text-link" href="/admin/security">Review security →</a></article>'
    elif section == 'connections':
        fieldset = hidden(csrf, status['revision'])
        configuration = '<div class="provider-actions"><form method="post" action="/admin/meta/connect">' + fieldset + '<button class="button primary" data-loading-label="Preparing connection…">Connect Facebook and Instagram</button></form><p class="hint">Reconnect through Facebook Login to verify the allowlisted accounts.</p></div><details class="credentials-panel"' + ('' if status['meta_secret_configured'] else ' open') + '><summary>Meta application credentials <span class="muted">' + ('Secret configured' if status['meta_secret_configured'] else 'Setup required') + '</span></summary><form method="post" action="/admin/meta/settings" class="settings-form">' + fieldset + '<div class="form-grid"><label for="app-id">Facebook App ID<input id="app-id" required name="app_id" maxlength="64" inputmode="numeric" value="' + escape(status['meta_app_id'], quote=True) + '"><small>Use the main Facebook App credentials.</small></label><label for="app-secret">App Secret<input id="app-secret" type="password" name="app_secret" maxlength="256" autocomplete="new-password" placeholder="Leave blank to keep the saved secret"><small>The saved secret is never displayed.</small></label></div>' + notice('Changing credentials disables reads, revokes the old connection and requires fresh account verification.', 'warning') + '<button class="button secondary" data-loading-label="Saving securely…">Save Meta settings</button></form></details><div class="read-control"><div><h3>Live account reads</h3><p>Read connected account content without modifying it.</p></div>' + badge('On' if status['live_reads'] else 'Off', 'success' if status['live_reads'] else 'neutral') + '<form method="post" action="/admin/meta/reads">' + fieldset + '<input type="hidden" name="enabled" value="' + ('false' if status['live_reads'] else 'true') + '"><button class="button secondary" data-loading-label="Saving…">' + ('Disable' if status['live_reads'] else 'Enable verified') + ' account reads</button></form></div>'
        body += provider_card(ProviderView('Meta', 'Facebook Pages and Instagram professional accounts', meta_accounts, configuration))
        if yt:body += youtube_card(yt,csrf)
        body += '<div class="empty-state future-note"><h2>One place for your connections</h2><p>Only implemented providers are listed. Additional integrations can appear here as they become available.</p></div>'
    elif section == 'features':
        if not status['live_writes']: body += notice('Publishing is paused by the current configuration. Review availability and effective state before changing a preference.', 'warning')
        groups = sorted({category(f['name']) for f in feature_status['features']})
        options = lambda values, selected: ''.join('<option value="' + escape(value, quote=True) + '"' + (' selected' if value == selected else '') + '>' + escape(label) + '</option>' for value, label in values)
        rows, count = feature_rows(feature_status, csrf, query, selected_category, selected_state)
        body += '<section class="card feature-panel" aria-label="Feature management"><form method="get" action="/admin/features" class="filter-bar" data-feature-filters><label class="search-field" for="feature-search"><span class="sr-only">Search features</span><input id="feature-search" name="q" type="search" maxlength="120" placeholder="Search features…" value="' + escape(query, quote=True) + '"></label><label for="feature-category"><span class="sr-only">Filter by category</span><select id="feature-category" name="category">' + options([('', 'All categories')] + [(g,g) for g in groups], selected_category) + '</select></label><label for="feature-state"><span class="sr-only">Filter by status</span><select id="feature-state" name="state">' + options([('', 'All states'),('enabled','Enabled'),('disabled','Disabled'),('unavailable','Unavailable')], selected_state) + '</select></label><button class="button secondary filter-submit">Filter</button></form><div class="table-meta"><output id="feature-count" aria-live="polite">' + str(count) + ' of ' + str(len(feature_status['features'])) + ' features</output><button type="button" class="text-button" data-clear-filters>Clear filters</button></div><table class="feature-table"><thead><tr><th scope="col">Feature</th><th scope="col">Category</th><th scope="col">Configured</th><th scope="col">Availability</th><th scope="col">Effective</th><th scope="col">Control</th></tr></thead><tbody>' + rows + '</tbody></table><div class="empty-state" id="feature-empty"' + (' hidden' if count else '') + '><h2>No matching features</h2><p>Try a different search or category.</p><a class="text-link" href="/admin/features">Show all features</a></div></section><div class="legend-grid"><article><h2>Configured</h2><p>Your saved preference, or the deployment default when unset.</p></article><article><h2>Available</h2><p>The deployment permits this capability and its required dependencies.</p></article><article><h2>Effective</h2><p>The preference and all current restrictions allow this feature to run.</p></article></div>' + notice('Enabling a feature does not approve a post. Disabling stops new execution but cannot recall a request already sent to Meta. Read-only reconciliation stays available.')
    elif section == 'security':
        rows = [('Administrator', 'Verified · recent MFA required'), ('Publishing permission', 'Granted' if status['publishing_permission'] else 'Not granted'), ('Approval policy', status['approval_mode']), ('ChatGPT Direct Publishing', 'On' if features['chatgpt_direct']['effective'] else 'Off'), ('Live-writing gate', 'Available within account restrictions' if status['live_writes'] else 'Paused')]
        body += '<article class="card"><div class="card-heading"><h2>Access &amp; publishing policy</h2>' + badge('Protected', 'info') + '</div><dl class="detail-list">' + ''.join('<div><dt>' + escape(k) + '</dt><dd>' + escape(v) + '</dd></div>' for k,v in rows) + '</dl></article>' + notice('ChatGPT direct mode relies on your explicit chat instruction through the authenticated client. SocialHub cannot independently verify that instruction as a consent receipt.', 'warning') + '<div class="two-column"><article class="card"><h2>Always enforced</h2><ul class="check-list"><li>Existing administrator identity and recent MFA</li><li>Publisher permissions and OAuth scopes</li><li>Allowlisted accounts and immutable content previews</li><li>Idempotency, replay protection and audit logging</li><li>Same-origin forms and session-bound CSRF protection</li></ul></article><article class="card"><h2>Feature access is not consent</h2><p>Switching on a capability does not publish, approve or schedule anything. Each consequential action follows its existing approval policy.</p><a class="text-link" href="/admin/features">Review feature controls →</a></article></div>'
    else:
        body += '<article class="card"><h2>Application preferences</h2><p>Dark mode is the default. This lightweight dashboard uses local assets and server-rendered pages.</p><dl class="detail-list"><div><dt>Feature preferences</dt><dd>Persistent · applied without restarting</dd></div><div><dt>AI disclosure</dt><dd>Reviewed with each immutable Reel preview</dd></div><div><dt>Configuration ownership</dt><dd>Administrator preferences within deployment security limits</dd></div></dl></article><div class="two-column"><a class="card settings-link" href="/admin/connections"><h2>Connection settings →</h2><p>Provider credentials, verification and live reads.</p></a><a class="card settings-link" href="/admin/features"><h2>Feature preferences →</h2><p>Control the implemented capabilities available to your accounts.</p></a></div><div class="empty-state"><h2>A place to grow</h2><p>Future application-wide settings will appear here when implemented. There are no additional settings to configure yet.</p></div>'
    return layout(body, section=section, connection=connection, csrf=csrf)


def youtube_card(status,csrf):
    fieldset=hidden(csrf,status['revision'])
    config='<div class="provider-actions"><form method="post" action="/admin/youtube/connect">'+fieldset+'<button class="button primary" data-loading-label="Preparing connection…">'+('Reconnect' if status['connected'] else 'Connect')+' YouTube</button></form><form method="post" action="/admin/youtube/disconnect">'+hidden(csrf)+'<button class="button secondary">Disconnect locally</button></form></div>'
    config+='<details class="credentials-panel"><summary>Google OAuth application credentials</summary><form method="post" action="/admin/youtube/settings" class="settings-form">'+fieldset+'<label>Google Client ID<input required name="client_id" maxlength="240" value="'+escape(status['client_id'],quote=True)+'"></label><label>Google Client Secret<input type="password" name="client_secret" maxlength="256" autocomplete="new-password" placeholder="Leave blank to keep saved secret"></label><p>The saved secret is never displayed. Changing settings requires reconnection.</p><button class="button secondary">Save Google settings</button></form></details>'
    config+='<dl class="detail-list"><div><dt>Verified channel ID</dt><dd>'+escape(status['channel_id'] or 'Not connected')+'</dd></div><div><dt>Handle</dt><dd>'+escape(status['handle'] or 'Not connected')+'</dd></div><div><dt>OAuth authorization</dt><dd>'+('Verified with offline refresh' if status['connected'] else 'Not authorized')+'</dd></div></dl>'
    if status['warning']:config+=notice(status['warning'],'warning')
    if not status['enabled']:config+=notice('YouTube is disabled by deployment policy. Configure its channel allowlist before connecting.','warning')
    accounts=(AccountView(status['name'],'YouTube',status['channel_id'],status['connected']),)
    return provider_card(ProviderView('YouTube','Google / YouTube · independent channel authorization',accounts,config,bool(status['connected'] and status.get('avatar'))))
