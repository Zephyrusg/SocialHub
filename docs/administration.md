# Setup with your existing identity

SocialHub has an optional, small setup page at **`/admin/`**. It is separate from ChatGPT and from the optional publication approval portal. Use it for setup and connection maintenance; normal posts, edits and replies still belong in ChatGPT under the configured approval policy.

There is no SocialHub password/user database to enroll into. The administrator signs in through the existing OAuth/OIDC provider with the existing account and MFA. Configuration must explicitly allow that account's verified stable subject. The feature is disabled by default.

## What the page can do

- Show whether the administrator identity also has publishing permission, the current write gates and approval mode.
- Save the main Facebook App ID and App Secret, encrypted in the existing database using the project's encryption key. A blank secret field preserves its current value; secrets are never returned to the page.
- Issue the secure one-use Facebook Login handoff for the already allowlisted Facebook/Instagram accounts. The page offers a local continuation link; it does not relax account restrictions or ask for credentials in chat.
- Show stored connection/expiration state and enable or disable reads after both accounts have been verified. No Meta request is performed merely by switching the read setting.

Changing Meta credentials disables reads, revokes the old stored platform credentials and invalidates pending Meta logins. Configuration revisions reject stale/concurrent forms and prevent an in-flight old-configuration OAuth login from storing credentials after the setup has changed. An already-dispatched external request cannot be recalled by changing settings.

The page cannot edit identity allowlists, grant itself publishing permission, change the issuer/origin/encryption key, enable public writes, approve a publication, execute content or bypass per-action consent. It exposes no new MCP tool.

## Administrator and publishing permissions

Grant the existing owner both permissions deliberately:

- `ADMIN_ALLOWED_SUBJECTS`: identities allowed into setup through the separate admin OIDC client.
- `PUBLISHING_ALLOWED_SUBJECTS`: identities permitted to prepare and execute the live-account publishing workflow in OAuth mode.
- `OAUTH_ALLOWED_SUBJECTS` and `OAUTH_ALLOWED_CLIENT_IDS`: still independently restrict MCP access.

A publisher still requires the `socialhub:publish` scope, enabled operator write gates, exact account/content/state checks and the configured per-operation approval. An administrator is not automatically a publisher. Local bearer development retains its existing test behavior; it is not a production authorization mechanism.

If the provider uses pairwise subject identifiers, the same person may have different subjects for admin and MCP clients. Bind each verified client/subject explicitly; do not infer the relationship from a display name, email, group argument or model claim. The role display refers to the currently authenticated admin subject; the separately bound MCP subject must also have its intended permission. No role grants Linux sudo, Docker control or database-root access.

## Bootstrap and rollout inputs

1. Back up the database/auth configuration and run additive migrations through **0006** before starting the updated service. The migration preserves existing tables/data and adds administration sessions, encrypted setup configuration and Meta-flow revision binding.
2. Register a **separate confidential admin OIDC client** with the existing identity provider. Use code + PKCE S256, `client_secret_basic`, `openid`, and two-factor authentication. The exact callback is `<BROWSER_ORIGIN>/admin/oidc/callback`. This is a machine/client registration, not a new human user.
3. Configure protected local environment values:

```dotenv
ADMIN_ENABLED=true
ADMIN_OIDC_CLIENT_ID=REGISTERED_ADMIN_CLIENT
ADMIN_OIDC_CLIENT_SECRET=GENERATED_PRIVATE_VALUE
ADMIN_ALLOWED_SUBJECTS=["VERIFIED_ADMIN_SUBJECT"]
PUBLISHING_ALLOWED_SUBJECTS=["VERIFIED_OWNER_PUBLISHER_SUBJECT"]
```

These are placeholders, not credentials. Do not reuse the MCP client as the admin client. Configure exact HTTPS issuer/JWKS/browser origin and trusted proxy sources using the existing settings. Enabling administration fails closed if its own credentials/allowlist are absent or its client is also allowlisted for MCP.

4. Route `/admin/` to the application through the reviewed proxy. Preserve the existing Host/HTTPS boundary, disable sensitive query/referrer/error logging at every hop, and never attach a browser `auth_request` redirect to MCP or discovery. Keep the approval portal off if unused; administration does not depend on `OWNER_OIDC_ENABLED` or `APPROVAL_PORTAL_ENABLED`.
5. Visit `/admin/oidc/login`, authenticate with the existing account, and use the setup forms. The administrator client must provide a signed `auth_time` for the requested maximum login age and a bounded AMR array proving either password plus TOTP (`pwd`, `otp`) or password plus passkey (`pwd`, `pop`, `mfa`). The passkey combination follows [Authelia's authentication references](https://www.authelia.com/reference/guides/authentication-method-references/); hardware/software and user-presence markers may also be present. Password-only, passkey-only, generic `mfa` without the independent factors, and malformed claims are rejected. Other MFA combinations require a separately tested policy extension. The optional approval portal retains its separate password/TOTP policy.
6. Keep `META_LIVE_WRITES=false`, `PUBLISHING_ENABLED=false` and `APPROVAL_MODE=owner_required` for the initial rollout. Granting the owner publishing permission does not activate writes or replace the approval-policy acceptance process.

## Session and secret handling

Admin sessions expire after 15 minutes. Login requires authentication no older than 15 minutes, including MFA. This is a privileged browser session, independent of ChatGPT's desired longer-lived OAuth connection/refresh behavior. Administrator logout revokes the server session; it does not claim to revoke every IdP/MCP token.

Sessions are hashed in the DB, cookie-bound and invalidated when the bootstrap admin policy changes. Cookies are Secure, HttpOnly, host-only and SameSite=Lax. State, nonce, PKCE, exact issuer/client, purpose and callback binding prevent token/flow confusion with MCP or the optional approval portal. Forms require authenticated same-origin requests, CSRF and exact bounded fields. HTML is escaped, responses are not cached, scripts/frames are prohibited, and failures do not echo credentials/provider exceptions.

The admin pages use `Referrer-Policy: strict-origin`: query-bearing URLs are not sent in referrers, while normal browser forms retain the Origin header required by the CSRF boundary. The [Fetch Standard](https://fetch.spec.whatwg.org/#origin-header) explains why `no-referrer` produces a null Origin for these form requests. Do not “fix” that browser behavior by accepting null origins.

Managed Meta values in `site_configuration` override their environment defaults. Bootstrap role/auth/write settings remain local environment configuration. Keep the encryption key and DB recoverable together; rotating the key requires a deliberate migration of stored settings and platform credentials. The CLI Meta helpers read the same effective configuration through `MetaOAuth`; external scripts constructing their own `Hub` should use `EffectiveSettings` if they need runtime preference changes.

## Local verification

Normal automated tests use temporary databases, synthetic signing keys and fake Meta transports. A real local Authelia/Chromium lab is also provided; it uses synthetic accounts and loopback-only ports, a separate Compose project/network and no production endpoints.

With the existing optional browser tools prepared as described in the development guide:

```bash
.tools/oidc-venv/bin/python scripts/admin_lab.py prepare
docker build --target runtime -t socialhub:admin-dev .
docker compose --project-name socialhub-admin-lab -f deployment/admin-lab/compose.yml up -d
docker compose --project-name socialhub-admin-lab -f deployment/admin-lab/compose.yml up -d --wait authelia
.tools/oidc-venv/bin/python scripts/admin_lab.py enroll
docker compose --project-name socialhub-admin-lab -f deployment/admin-lab/compose.yml exec -T socialhub alembic upgrade head
.tools/oidc-venv/bin/python scripts/verify_admin_lab.py
```

The browser verifier inherits the platform library/font environment described in the development guide. It validates a real signed synthetic identity before binding the lab admin subject, then tests password/TOTP login, both owner permissions, form submission, secret redaction, persistence across only the disposable app's recreation, nonadmin denial and logout. Nonlocal browser requests are blocked. It does not click through to a real Meta login or publish anything. Generated users, passwords, seeds, keys and identity mappings stay in ignored `secrets/admin-lab/`; safe results go into ignored `data/admin-lab-evidence.json`.

Stop only this disposable lab with `.tools/oidc-venv/bin/python scripts/admin_lab.py down`. This does not stop an existing staging route. No test/browser dependencies were added to the production runtime dependency set.

See the [recorded validation results and resource measurements](administration-validation.md) for the completed local checks and remaining rollout gates.

## Rollback and limits

Disabling `ADMIN_ENABLED` closes setup access but does not discard encrypted settings already saved in the DB. Back up and reconcile those settings before attempting a configuration reset. A downgrade that drops administration tables loses saved setup preferences; do not run one automatically. Older images have a different readiness-schema expectation, so an image-only rollback is not a complete schema compatibility plan. On a first rollout, stopping the new app/route is the safe fallback.

Media concurrency/quota hardening and ChatGPT refresh-token integration remain separate work. This feature is setup administration, not an approval portal or an automatic production deployment.
