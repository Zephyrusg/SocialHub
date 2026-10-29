# Administration dashboard

SocialHub's existing setup interface now uses a lightweight dark dashboard with
local HTML/CSS and a small JavaScript filter. It requires the same administrator
identity and recent MFA as before. It does not introduce another account system
or a publication approval step.

| Page | Purpose |
| --- | --- |
| `/admin/` | Service/database reachability, stored account connections and publishing overview. |
| `/admin/connections` | Provider cards, protected Meta application settings, reconnect handoff and account-read controls. |
| `/admin/features` | Searchable and filterable feature preferences, deployment availability, effective state and switches. |
| `/admin/security` | Current authorization and approval policy, with its limits explained. |
| `/admin/settings` | General application information and links to implemented settings. |

All account connection indicators use stored account/credential state, not an
automatic live provider request. Provider and account presentation components
support multiple accounts and future providers without pretending unsupported
integrations are available. Meta remains the only implemented provider.

## Feature controls

Configured, available and effective states remain separate. A configured feature
can be ineffective because a parent switch or deployment restriction prevents
it from running. Explanations remain visible alongside the effective state.
New Instagram media normalization correctly shows its off default; scheduling
remains unavailable. See [feature flags](feature-flags.md) for backend semantics.

Search and category/status filtering run locally when JavaScript is available.
The GET filter form also works without JavaScript. No client-side script writes
settings through a separate API: switches submit the existing authenticated,
CSRF-protected forms. Sensitive publishing enables retain the server-generated
confirmation page, and stale revisions are rejected. Success/error feedback and
loading labels make the outcome explicit. Failed configuration changes provide
a settings review path without requiring a new login for every error.

## Preserved boundaries

- Existing OIDC login, MFA, administrator/publisher roles and logout remain.
- Same-origin forms, exact field validation, session-bound CSRF tokens and
  revision checks remain server-authoritative.
- App Secrets are never rendered; a blank replacement preserves the saved secret.
- No feature preferences, credentials, scopes, approval policies, account
  restrictions or publication requests change merely by viewing the dashboard.
- MCP operations, media import/delivery/conversion and publishing continue to
  use their existing service implementations.

Styles and scripts are served from a fixed asset whitelist under `/admin/assets/`.
No arbitrary file paths, CDN, remote fonts or frontend framework are used. Assets
contain no session data or configuration. The content security policy allows only
same-origin styles/scripts and forms, with framing, objects and base overrides
blocked. Pages and assets retain no-store responses. Content-derived asset version
parameters prevent a release from reusing stale CSS/JavaScript.

## Responsive and accessible behavior

Desktop navigation uses a sidebar. Phone layouts use compact navigation and
feature cards instead of a wide table. A sign-out control stays available on
mobile. The interface includes active navigation, skip-to-content, labelled form
fields and switches, keyboard focus indicators, live filter counts and reduced
motion support. General Settings contains no invented operational controls.

Browser validation covers Overview, Connections and Feature Flags at 1920×1080,
1366×900, 390×844 and 320×740, with no horizontal overflow. Private visual evidence
uses synthetic account fixtures. Browser checks exercise filtering/empty states,
toggle persistence, publishing warnings/cancel/confirmation, credential redaction,
Meta settings submission, read toggles and the local OAuth handoff without following
the Meta link. Existing automated authorization, OAuth, CSRF, storage and MCP
regressions remain required.

## Deployment and rollback

Build and test the standard multi-stage image. `pyproject.toml` includes the two
static assets in the application wheel; no production dependency was added.
Verify their presence using the installed package as well as the source tree.

Back up the current image/source and scoped Compose configuration, check that no
publication send or media transfer is active, then replace only the SocialHub
image and recreate that service. Keep every runtime feature preference, secret,
worker setting and existing network unchanged. The existing `/admin/` proxy prefix
already includes the new pages and assets; no additional public endpoint is needed.

Validate public static asset delivery, the protected administrator page, MCP
authentication challenge and OAuth discovery through the external route. Do not
forge a production administrator session for browser screenshots; use the isolated
test fixture and let the owner check their existing login normally.

Rollback restores the previous **normalization-capable** SocialHub image and its
source/Compose image reference. Keep the normalizer worker, its secret, database
and all runtime preferences intact. No database migration or preference cleanup
is needed for this UI-only rollback. Exact deployment/rollback commands and
operational evidence belong in ignored `private/` change tracking.
