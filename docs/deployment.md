# Deploying from a Git checkout

A Git clone can deliver SocialHub's source and deployment definitions. It does not include a database, media, production credentials or the operator's ignored `private/` folder. Pulling new source does not update an existing container, because the production template selects a built image rather than bind-mounting the application source.

## Development versus production

`compose.yml` is a local Docker Desktop/WSL example. It expects an existing MariaDB instance, an external local Docker network, generated localhost TLS and an ignored environment file. It is not a production installer. The application can also be tested independently using temporary SQLite databases and fake adapters; see the README.

`compose.production.yml` is a review template for one `linux/arm64` application container. It publishes no application host port and assumes separately managed reverse-proxy and database networks. The sample `.test` origin, source addresses and paths are reserved/example values, not a working public endpoint. Review and adapt all of them through local operator configuration before use. Existing services should be reused where appropriate, not replaced by this project.

## Required production preparation

1. Choose a reviewed source commit and build/test its ARM64 image on an appropriate builder. Deliver through an approved registry or protected transfer, then select the immutable image digest. Avoid compilation on a small Pi.
2. Establish protected, consistent database/auth-state backups and demonstrate recovery before shared changes. Scheduled backup automation is a separate operational task.
3. Provision a dedicated `socialhub` database with a DML-only runtime user and a separate namespace-limited DDL migrator. Verify actual source addresses and use parameterized administration, not passwords in command lines or Git. Review `deployment/production/database.sql.example`; its source IPs are examples.
4. Generate fresh production keys and ignored `secrets/runtime.env`. Do not copy development Meta tokens or synthetic OIDC users. Set persistent media-directory ownership for UID 10001; keep media disabled initially. Apply migrations through 0006 with the separate migration credential and check readiness/schema drift.
5. Configure a compatible OAuth/OIDC provider and pre-registered MCP client, exact HTTPS issuer/JWKS/audience/redirect, owner/client allowlists and MFA. A separate confidential owner client is required if enabling the independent portal. Authelia 4.39.28 was tested; 4.39.22 failed resource-correct token exchange during development.
6. Adapt the example Nginx virtual host and provider fragment to the actual infrastructure. The example proxy includes are placeholders for an existing trusted TLS/source-guard setup. Check every proxy hop for sensitive-query logging; disabling logs at the application alone is insufficient.
7. Start only the reviewed SocialHub service after checks and authorization. Verify 401 discovery challenges, OAuth, tool reads, streaming, request limits, private-route restrictions and account boundaries. Measure native idle/tiny-fixture resource use. Do not infer native performance from ARM64 emulation.
8. Complete fresh Meta onboarding through a secure local administration/OAuth procedure and verify only the intended accounts read-only. Do not send credentials through chat.

Keep the default owner-required policy, live-write gates and optional media disabled throughout the initial rollout. Token refresh/reauthorization behavior needs an actual client test; the development fixture uses short-lived tokens without refresh and is not a durable production session configuration.

## Updates and rollback

Review the release, pull its source, build/test/deliver the corresponding image elsewhere, update the chosen digest, apply explicitly required migrations and recreate only SocialHub. Do not attach automatic deploy hooks to unreviewed commits. Test the resulting route and keep an image/configuration rollback record.

For a failed first deployment, stop only SocialHub and remove/restore only its newly added proxy route, validate Nginx, then gracefully reload. Do not remove shared volumes, restore the whole shared DB blindly, run global pruning or tear down unrelated Compose projects. Database migration rollback needs an explicit compatibility/data plan.

The examples require operator decisions. They are not authorization to change an existing Raspberry Pi, VPS, database or identity provider.

## Optional setup administration

See [existing-identity administration](administration.md) for the separate confidential admin client, role allowlists and `/admin/` routing. The existing owner can hold admin and publishing permissions together. Migration 0006 is required; admin access is disabled by default and does not activate writes or require the optional approval portal.
