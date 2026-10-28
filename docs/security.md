# Security and operating boundaries

SocialHub is a single-owner project. Its live Meta allowlist deliberately identifies the two public PixelPapa accounts in `app/connectors/meta_policy.py`. Those public identifiers and the artist handle remain in the source; infrastructure details, credentials and personal test records do not belong in public Git history.

## Authentication and approval

Production requires HTTPS and OAuth. The resource server validates RS256 access tokens with `typ=at+jwt`, exact issuer/resource audience, expiry, permitted client and permitted owner subject. An OAuth scope is a permission boundary, not proof that a person approved the precise text or action.

Keep `APPROVAL_MODE=owner_required`, `META_LIVE_WRITES=false`, `PUBLISHING_ENABLED=false` and the native-confirmation/risk flags false for an initial read-only deployment. The optional independent owner portal and administrative CLI issue grants bound to immutable content hashes. Write tools remain marked as writes; unknown remote outcomes stop for reconciliation instead of automatic resubmission.

ChatGPT's native confirmation cannot currently be independently verified by this server. The alternative policy mode requires explicit operator activation after actual host-confirmation testing and acceptance of that residual risk. Do not substitute a model-provided approval flag, username or client name.

## Secrets and private material

Generate separate secrets for each environment. Keep environment files, keys, databases and media in ignored `.env`, `secrets/` and `data/` paths with restrictive permissions. Meta tokens are encrypted at rest; encryption-key recovery needs a separate protected backup. `.gitignore` does not encrypt files or protect against `git add -f`.

Local operational documents and personal test records may live in `private/`. Both Git and Docker ignore this folder. A public clone will not contain it. Do not attach it to bug reports or publish the original private development history. Only the clean release checkout should supply public commits.

## Media limitation

Authenticated media ingestion now has cross-process admission before receiving bytes, per-file/storage/file-count/free-space limits, whole-transfer deadlines and automatic expiry/orphan cleanup. Attachment receivers pin public DNS addresses, verify TLS hostnames, reject redirects and never forward OAuth credentials. Input-schema validation hides capability URLs. JPEG/PNG decoding is pixel-bounded; MP4 validation uses a resource-limited metadata-only ffprobe worker. These controls are covered by synthetic tests, but actual ChatGPT image/MP4 transfer and controlled live Meta media acceptance remain activation gates. Keep production media flags/routes off until the required checks and explicit authorization. See [Facebook media](facebook-media.md).

## Proxy and deployment controls

Publish only the reviewed HTTPS reverse-proxy route. Do not expose the container's development bearer endpoint publicly. Validate proxy source addresses, forwarding headers, Host/Origin boundaries, sensitive-query log privacy, streaming and request limits. OAuth codes, cookies, media signatures and tokens must not appear in logs.

Use a nonroot container, read-only filesystem, private data volume, explicit CPU/RAM/process limits, separate runtime/migration DB users, consistent backups and a tested rollback. The application cannot enforce restrictions configured incorrectly at an outer proxy.

## Review status

The pre-release review covered the original locally available 14-commit history, tracked files, authentication/approval/media boundaries, an offline secret scan, static analysis and the locked Python dependencies. No confirmed real credential leak was found; 44 pinned Python packages had no known vulnerabilities reported by pip-audit on 27 October 2026. These are bounded observations, not a guarantee. Complete container OS/native-binary auditing and license review remain separate work.

Never post a credential, private key, personal post record or full OAuth URL in a public issue. For a suspected leak, stop the affected exposure, use a private communication channel and rotate the affected credential through local administration.

## Setup administration

The optional `/admin/` UI uses an independent confidential OIDC client, recent password plus TOTP or password plus passkey MFA, hashed short-lived sessions and same-origin/CSRF-protected bounded forms. Passkey acceptance requires signed `pwd`, `pop` and `mfa` markers; single-factor and malformed claims fail closed. This admin-only extension does not change the optional approval portal's MFA policy. Exact bootstrap subjects govern admin and publisher permissions separately; OAuth-mode live publishing additionally requires the explicit publisher allowlist. Managed Meta settings are encrypted and revision-bound; changing credentials revokes connections/pending Meta flows and disables reads. The UI cannot grant roles, exceed deployment write ceilings or approve posts. Its encrypted, revision-bound [feature preferences](feature-flags.md) can pause/resume only already permitted capabilities; audit and preference changes commit atomically. Unknown configuration fails closed, and GET-only reconciliation remains possible while mutations are paused. See [administration](administration.md).

Origin rejection diagnostics contain only fixed reason categories and whether the request targeted administration. They never record supplied origin values, request bodies, URLs, cookies or credentials. Diagnostics do not relax the exact-origin boundary.

Real-account writes additionally require the explicit `META_WRITE_ACCOUNT_IDS` allowlist (empty by default). Facebook-only preparation and the disabled-write host confirmation test are documented in [Facebook write readiness](facebook-write-readiness.md). Account permission is separate from an owner grant and from the two global publishing switches.

An explicitly accepted [chat-first policy](chat-first-approval.md) may rely on either tested native host confirmation or separately tested direct user instructions through an exact authenticated client. Both expose that consent is not independently verified; neither can be activated by a model-supplied approval flag. The default independent-owner policy is unchanged.
