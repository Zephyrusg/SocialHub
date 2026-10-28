# Reel deployment and rollback

The owner-approved code-only rollout completed on 28 October 2026, with Reel
publishing disabled. The application image changed; runtime settings, approval
policy, credentials and database schema did not. Native ARM64 validation and the
owner's actual ChatGPT read-only capability test passed. See the
[validation report](reels-validation.md). The owner subsequently approved
Facebook-Reel-only controlled test activation, retaining disabled Instagram,
image and regular-video writes. The separately approved Facebook export-validation
correction is also deployed.
No live Reel test has been performed.

This procedure does not itself authorize future production changes or live Reel
publication. Obtain explicit approval for the exact rollout/settings and each
controlled Facebook, Instagram and combined live test. Existing production
Facebook text publishing and `chatgpt_direct` remain unchanged.

## Build and development validation

```bash
uv sync --locked --extra test
uv run --locked --extra test pytest -q
docker buildx build --platform linux/arm64 --target runtime --load -t socialhub:REVIEWED_COMMIT .
docker buildx build --platform linux/amd64 --target test --load -t socialhub:reels-test .
docker run --rm --network none --read-only --no-healthcheck \
  --tmpfs /tmp:size=256m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges socialhub:reels-test
```

Use the existing pinned prebuilt ffprobe and uv multi-stage build/cache. Do not
compile FFmpeg or add an encoder/test dependencies to runtime. Runtime native
binary provenance/security review remains a separate obligation; this release
does not claim to patch the previously deferred ffprobe/Brotli concern. Record
the reviewed source commit and platform-specific image digest before deployment.

The test-only `/tmp` capacity must exceed the media pipeline's existing 64 MiB
free-space reserve plus its fixtures/databases. A 64 MiB tmpfs cannot satisfy that
guard and can stall tests waiting for an upload that was correctly refused. The
256 MiB test capacity is sparse, not an allocation of that much RAM. Production
keeps its smaller `/tmp` and the existing separate HDD media directory.

## Production preparation after approval

1. Record current Git revision, ARM64 image/digest, Compose path, root-only runtime
   env path, policy flags, database migration head and health. Back up runtime
   env and Compose into an owner-restricted ignored operational directory. Keep
   secrets out of console output, Git and container build context. Record
   unrelated services' IDs/start times to verify they were not restarted.
2. Verify the existing private HDD media mount, permissions and free space. Reuse
   that mount without creating another media directory or an SSD-backed fallback.
3. Build the tested ARM64 runtime from Git, transport it by the existing reviewed
   image procedure, and fast-forward the production checkout to the reviewed
   commit. No schema migration is introduced by Reels. Do not hot-patch code.
4. Initially keep `INSTAGRAM_REELS_PUBLISHING_ENABLED=false` and keep `reel` out
   of `FACEBOOK_MEDIA_ALLOWED_TYPES`. Recreate **only SocialHub** with the existing
   reviewed Compose project/service selection. Do not restart Authelia, Nginx,
   MariaDB or unrelated containers. No new OAuth scope, callback, client, Nginx
   route, firewall rule, VPS configuration or public media URL is required.
5. Validate readiness, existing text permissions, authenticated MCP discovery,
   owner restrictions, cached publication history and unchanged global policy.
   Run `python scripts/verify_reels_readiness.py` in the container. Do not infer
   upload-session eligibility from this GET-only check.
6. Refresh ChatGPT tools. Import a fresh actual Flow MP4 and prepare previews for
   Facebook, Instagram and both. New metadata rules require re-importing older
   assets. Preparation can require a separately approved write-account allowlist
   expansion for Instagram; it must not activate publishing. Confirm exact media
   hash/caption/accounts and disabled media writes before any execute call.

## Explicit activation gates

| Purpose | Required settings, in addition to existing global write/role/client/approval checks |
| --- | --- |
| Facebook Reel only | Approved Facebook account in `META_WRITE_ACCOUNT_IDS`; `FACEBOOK_MEDIA_PUBLISHING_ENABLED=true`; `FACEBOOK_MEDIA_ALLOWED_TYPES=["reel"]` |
| Instagram Reel only | Approved Instagram account in `META_WRITE_ACCOUNT_IDS`; `INSTAGRAM_REELS_PUBLISHING_ENABLED=true`; keep Facebook media disabled |
| Both | Both account IDs and both preceding Reel gates |

Adding Instagram to the account allowlist does not enable Instagram image
publication. `MEDIA_DELIVERY_ENABLED` and a public media base are not required
for Reel binary uploads. Do not enable other content types while authorizing a
Reel-only test. Preserve existing global settings and the exact authenticated
`chatgpt_direct` policy/owner restriction. Do not change independent-owner policy
defaults or assert native ChatGPT confirmation has been independently verified.

Run controlled tests in order: Facebook only, Instagram only, combined. Each
requires the owner's exact media/caption/account instruction. Within each test,
advance the same publication ID; honor retry times and read-only polling. Verify
each final remote ID, caption, media and permalink independently before reporting
success. Never replace or repeat an uncertain target. If resumable Instagram
session creation is rejected, stop and resolve app eligibility; do not introduce
a hosted URL fallback without a separately reviewed design. Deletion/cleanup of
published content requires its own explicit authorization.

After a test, disable its media gate unless the owner approves continued
activation. GET-only processing/finalization remains available with write gates
disabled. Record sanitized results and exact applied paths/timestamps in ignored
`private/`, distinguishing application rollout from gate activation and live tests.

## Rollback

1. Stop new media mutations by disabling both platform media gates; preserve the
   existing Facebook text settings and owner policy. Recreate only SocialHub.
2. Record every in-flight target and its stage/remote ID. Continue GET-only
   reconciliation before reverting the image: older code cannot understand new
   Reel checkpoints safely. Do not execute an old image against those targets or
   reset/remove their records. Keep the new gates-off image for reconciliation
   if any target is uncertain or processing.
3. Once targets are terminal or independently reviewed, restore the backed-up
   Compose image pin and the reviewed prior runtime settings. Explicitly keep
   Facebook media disabled and remove/disable the Instagram Reel gate; restoring
   an old enabled media setting is not an authorization to publish. Recreate
   only SocialHub with the previous image already present locally.
4. Validate readiness, OAuth/MCP, unchanged text configuration, database/history
   and unrelated services. No database restore or migration downgrade is needed.
   Preserve audit/history and existing private media retention; do not delete
   remote content or media directories as a rollback step.

The private handoff records actual infrastructure paths, source/image IDs and
review evidence. The public examples remain sanitized. No homelab configuration
commit is required for this code-only rollout; if the future homelab repository
tracks the Compose/env template, record the reviewed image pin and explicit
default-off Reel flags there, never credentials or operational evidence.
