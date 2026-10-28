# SocialHub MCP

**A small, self-hosted bridge between your social accounts and an AI assistant—with you in control.**

SocialHub is a personal project built around **PixelPapa**, my music and artist project. The idea is simple: spend more time making music and less time switching between social dashboards. I want to review posts, prepare a release announcement and manage everyday social tasks in a conversation, while keeping the accounts, credentials and final decisions under my control.

If you arrived here for the code, welcome. If you also enjoy discovering music, look out for **PixelPapa**—on Instagram as **@pixelpapamusic**.

## What we're building

SocialHub exposes Facebook and Instagram tools through the Model Context Protocol (MCP). An assistant such as ChatGPT can use those tools to read connected accounts and posts, check account capabilities, and prepare exact content for review.

The longer-term goal is a comfortable workflow: ask for something, see precisely what will happen, approve it through the configured channel, and get a clear result. A generated sentence or a model-supplied “approved” flag is never treated as independent proof of permission.

This is an evolving single-owner project. The current live-account allowlist is deliberately specific to PixelPapa's accounts. It is not yet a general-purpose service where anyone can connect arbitrary accounts.

## What works today

- Facebook and Instagram read connectors using the Meta Graph API.
- Facebook post text, stories and photo/video attachment metadata through consistent [post content reads](docs/facebook-content.md).
- ChatGPT OAuth linking and MCP reads tested end to end against an isolated mock-data staging environment.
- Exact publication previews, content/media hashes and persistent per-account operation states.
- Publishing and supported edit/reply methods implemented behind feature flags and approval checks; coverage varies by operation and platform.
- Optional setup administration through your existing OIDC account, with separate admin/publisher permissions and encrypted Meta settings.
- Separate owner approval through an optional lightweight portal, with an administrative CLI fallback; setup does not require that portal.
- Private media ingestion, image validation and metadata-only video checks.
- Automated tests for authentication, approval boundaries, account restrictions, replay protection and failure handling.

Live Meta reads and narrowly authorized Facebook text and image tests have been verified. The image post's uncertain timeout was reconciled without retrying. That does **not** mean every publishing/media operation is live-verified, or that normal public writes are enabled. Video, Instagram publishing and broader management workflows still need separately authorized acceptance tests. Scheduling currently records mock jobs; there is no autonomous production publishing worker.

## Designed for a Raspberry Pi

The intended home is an ARM64 Raspberry Pi using existing Nginx, MariaDB and an OAuth/OIDC provider. Development happens in WSL, and builds/tests are kept off the Pi where possible.

The Docker build uses uv, pinned build inputs and separate test/runtime stages. Video validation uses a prebuilt ARM64-compatible **ffprobe** executable for metadata only. SocialHub does not transcode or re-encode media. Tests use tiny pre-generated fixtures; compilers, encoders and browser test dependencies do not belong in the production runtime.

The runtime runs as a nonroot user and supports a read-only root filesystem with explicit CPU, memory and process budgets. ARM64 tests have passed under emulation; native Pi resource acceptance remains a separate deployment step.

## Safety and ownership

SocialHub keeps live publishing off by default. The default policy is `owner_required`, with immutable request hashes, account restrictions, audit records and idempotency checks. Ambiguous remote outcomes stop for reconciliation rather than silently creating another post.

Meta credentials are encrypted at rest. Runtime secrets, private identity state and media are kept outside version control. Production OAuth validates signed tokens and exact issuer, audience, client and owner identities.

ChatGPT's own confirmation UI and a server-verifiable owner grant are different trust boundaries. Chat-first policy reliance remains gated behind explicit testing and operator acceptance; simply connecting ChatGPT does not enable it.

Media exposure and storage limits need further hardening before production activation. The temporary staging session also lacks automatic token renewal; that remains production preparation work. This project is still under development, so review the code and deployment assumptions before running your own instance.

## Working on the code

You need Python 3.12 or later and uv for the Python tests:

```bash
uv sync --locked --extra test
uv run --locked --extra test pytest -q
```

For the container test suite, including the bundled ffprobe validator:

```bash
docker build --target test -t socialhub:test .
docker run --rm --no-healthcheck --tmpfs /tmp:size=64m,mode=1777 socialhub:test
```

The Python tests use temporary databases and fake API transports; they do not require real Meta credentials or publish social content. Host-side video tests may skip when ffprobe is unavailable; the Docker test target includes it.

Acknowledged Meta deliveries finalize through GET-only metadata refresh; status polling does not publish again. See [delivery finalization](docs/delivery-finalization.md).

Phase 3D adds guarded ChatGPT attachment import, expiring private media and Facebook image/video workflows. Actual PNG and Google Flow MP4 imports/previews, signed external downloads and one live image publication have passed. Video publication still requires separate acceptance; see the [validation report](docs/facebook-media-validation.md), [timeout reconciliation](docs/facebook-media-timeout.md) and [Facebook media](docs/facebook-media.md).

Documentation: [development](docs/development.md), [deployment](docs/deployment.md), [setup administration](docs/administration.md), and [security boundaries](docs/security.md).

## Deployment direction

The intended route is:

```text
ChatGPT → HTTPS reverse proxy → secure tunnel → Pi reverse proxy → SocialHub
                                                                      ├─ MariaDB
                                                                      ├─ OAuth/OIDC provider
                                                                      └─ Meta Graph API
```

You can clone the repository onto a deployment host, but a clone is only the source/configuration checkout. The development Compose file is not a production installer. Production needs its own protected secrets, database users/migrations, OAuth clients, exact URLs, backups and a reviewed ARM64 image. Pulling new commits does not automatically replace the running container.

Keep read-only operation and disabled media as the initial rollout. Add live features only after their authentication, approval, storage and recovery requirements have been tested.

## Where we'd like to go next

Reliable session renewal, a measured Pi deployment, a smoother chat-first review experience, stronger media resource limits and carefully verified publishing workflows. The goal is useful personal tooling that stays understandable and maintainable on small hardware.

## Project status and license

Development project, not a production support promise. A project license has not yet been selected; choose and add one before presenting a public release as open source. Third-party dependencies and distributed binaries retain their own licensing requirements.

## Local operational records

Keep environment-specific infrastructure reports and personal test records in the ignored `private/` folder. It is also excluded from Docker build contexts and is not included in public clones. Public account identifiers in the intentional PixelPapa allowlist are retained; no account restriction has been relaxed.
