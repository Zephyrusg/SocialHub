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

Authenticated uploads have a per-file byte cap and validation checks, but admission concurrency, total storage quota, receive deadlines and stale-file retention need hardening. The current semaphore applies to validation after a request body has been received. Keep production media disabled and the upload route blocked until those controls are implemented and tested. Only small JPEG/PNG images and a narrow metadata-validated MP4 subset are supported. No transcoding is performed.

## Proxy and deployment controls

Publish only the reviewed HTTPS reverse-proxy route. Do not expose the container's development bearer endpoint publicly. Validate proxy source addresses, forwarding headers, Host/Origin boundaries, sensitive-query log privacy, streaming and request limits. OAuth codes, cookies, media signatures and tokens must not appear in logs.

Use a nonroot container, read-only filesystem, private data volume, explicit CPU/RAM/process limits, separate runtime/migration DB users, consistent backups and a tested rollback. The application cannot enforce restrictions configured incorrectly at an outer proxy.

## Review status

The pre-release review covered the original locally available 14-commit history, tracked files, authentication/approval/media boundaries, an offline secret scan, static analysis and the locked Python dependencies. No confirmed real credential leak was found; 44 pinned Python packages had no known vulnerabilities reported by pip-audit on 27 October 2026. These are bounded observations, not a guarantee. Complete container OS/native-binary auditing and license review remain separate work.

Never post a credential, private key, personal post record or full OAuth URL in a public issue. For a suspected leak, stop the affected exposure, use a private communication channel and rotate the affected credential through local administration.

## Setup administration

The optional `/admin/` UI uses an independent confidential OIDC client, recent password/TOTP MFA, hashed short-lived sessions and same-origin/CSRF-protected bounded forms. Exact bootstrap subjects govern admin and publisher permissions separately; OAuth-mode live publishing additionally requires the explicit publisher allowlist. Managed Meta settings are encrypted and revision-bound; changing credentials revokes connections/pending Meta flows and disables reads. The UI cannot grant roles, enable writes or approve posts. See [administration](administration.md).
