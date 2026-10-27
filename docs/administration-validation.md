# Administration validation

Validated in a disposable local lab on 2026-10-27. No production host was modified and no real Meta API request or social publication was performed.

## Results

- **254 tests passed** on the development host (9.12 seconds) and in the ARM64 test image under emulation (112.70 seconds). Both runs reported three Alembic configuration deprecation warnings.
- Real Authelia password/TOTP authentication and Chromium navigation passed with synthetic identities. The owner had both explicitly assigned roles; an authenticated nonadministrator was denied.
- Browser forms passed strict Origin and CSRF checks. Secrets were redacted, encrypted settings and sessions survived recreation of the disposable application, and logout revoked the session. The approval portal remained disabled.
- Additive migration 0005 → 0006 preserved existing test data. Configuration revision, stale OAuth flow, role separation and single-connection database-pool regressions are covered by automated tests.
- Production Compose and dedicated Nginx examples passed local syntax validation with stand-in services. This does not validate a particular production host's active configuration.

The browser test exposed two issues that were corrected: `no-referrer` produced a null Origin on form submission, and redirecting a form directly into Facebook conflicted with the restrictive form CSP. Setup now sends origin-only referrers under `strict-origin`, continues to reject null origins, and presents a local GET continuation link for Facebook Login.

## Runtime observations

The final ARM64 runtime used approximately **117.58 MiB of compressed image content**, with **326.58 MiB of uncompressed image layers**. These are different size measurements; registry/cache storage and installed disk usage can differ. An export archive was also prepared locally, outside Git.

The native development application used **74.68 MiB idle memory** and approximately **0.19% CPU** in a container limited to 384 MiB. This excludes the database, identity provider and proxies and is not a measurement on Raspberry Pi hardware.

Five metadata-only ffprobe runs on a pre-generated 5,622-byte, three-second fixture in an emulated ARM64 container observed:

| Measurement | Result |
| --- | --- |
| Median elapsed time | 489.542 ms |
| Median process CPU time | 154.858 ms |
| Average CPU usage, one core basis | 33.52% |
| Observed peak probe RSS | 40.684 MiB |

These small-fixture results do not establish limits for large or hostile uploads. Production media uploads remain subject to the separate concurrency/quota hardening work.

The runtime contains the prebuilt ffprobe binary for metadata validation. It contains no ffmpeg encoder executable, pytest, Playwright or PyYAML. No dependency was added to the production Python dependency set for administration or browser testing, and no FFmpeg source build was introduced.

## Security review and remaining acceptance

The staged source was checked for private operational paths, infrastructure identifiers and exact matches against locally available private values. No matches were found. Secret-scanner findings were reviewed as example placeholders, synthetic test values or generation code. This review is not a guarantee that the application has no vulnerabilities.

Administration remains disabled by default. Production still requires approved identity-provider client registration, exact verified subject bindings, proxy/privacy validation, database migration and native hardware acceptance. Existing owner-required per-operation approval remains enforced. Actual ChatGPT write-confirmation behavior and longer-lived MCP refresh sessions require their separate acceptance tests before enabling production publishing.

See [administration setup and rollback](administration.md) for the deployment inputs and limitations. Deployment and activation are separate from this local implementation.
