# YouTube integration and release procedure

YouTube is independent of Meta: a separate Google client, exact channel allowlist,
channel-bound encrypted credentials and provider-specific API client. Existing
Authelia administrator/publisher roles remain authoritative. Start with
[youtube-setup.md](youtube-setup.md) for Google Cloud configuration.

## Implemented development workflow

Import an allowed ChatGPT MP4 attachment once with `import_media`; reuse its media
ID. `prepare_publication` accepts one `pixelpapa-youtube` target, `content_type`
`video` or `short`, its `media_ids` and a structured `youtube` object:

```json
{
  "title": "Reviewed title",
  "description": "Reviewed description and #hashtags",
  "tags": ["music", "fantasy music"],
  "visibility": "private",
  "made_for_kids": false,
  "contains_synthetic_media": true
}
```

Audience and disclosure are explicit booleans. Private is the safe default;
Public/Unlisted are preserved if explicitly selected. YouTube metadata is not
stored in a Facebook caption. The immutable preview contains channel identity,
metadata, source hash and expected Shorts eligibility. A standard video might
also be classified as a Short by YouTube. This service cannot guarantee the
platform's classification or claim handling.

`execute_publication` follows the existing owner-required or authenticated
chat-directive policy. Host confirmation is not an independent consent receipt.
A feature switch never authorizes content. Each execution advances one bounded
upload step, not a background uploader. On `execute_next_step`, continue the
same publication only as part of the user's explicit reviewed instruction;
respect `retry_at`. `poll_status` means use `get_publication_status` (GET only).
Never manufacture a new publication, grant or session after uncertainty.

## Durability and resource bounds

The first step initializes Google's official videos.insert resumable session.
Its URL is encrypted inside the existing target checkpoint; it is omitted from
previews, results and audits. Upload steps first query that original session with
an empty PUT, validate Google's contiguous Range acknowledgement and then stream
one chunk from disk in 64 KiB buffers. The chunk size is persisted with the
session and cannot change midway; default 8 MiB, configurable multiples of
256 KiB within 256 KiB–16 MiB. One cross-process nonblocking file lock limits
active YouTube transfer steps to one on the shared media volume. No new queue,
database, browser or encoder is needed in production.

Interrupted chunks and lost final responses retain the same session. A later
explicit execution queries it and either resumes acknowledged bytes or recovers
the already-created video ID. A missing/uncertain initial session is never
reinitialized automatically. Expired sessions require manual outcome inspection;
SocialHub intentionally does not follow generic advice to create a new session
without first ruling out an existing video. Application crash recovery preserves
the claimed session; execution may report an expired lease once before a later
explicit continuation queries it. Attempts remain bounded (4096 YouTube steps,
20 existing Meta steps), with backoff and Retry-After. Successful source upload,
processing confirmation and actual Public visibility are separate fields.

Once a video ID is acknowledged, polling uses videos.list and verifies channel
ownership. Success means processing completed; it does not imply Public.
Actual privacy is returned beside requested privacy. Private restrictions are
never bypassed by an update call. Trusted local `reconcile_publication.py` can
inspect an owner-selected video ID using GETs, validating channel ownership,
exact structured metadata, original file size and execution time before atomic
local repair. It never sends another upload. Remote bytes are not hash-equated
to the source after YouTube processing.

Storage remains the existing bounded, private, expiring media volume. Existing
file/byte/free-space quotas and cleanup still apply. Files are protected during an actively claimed transfer lease and reserved
during delivery. TTL still applies while a workflow is paused between steps. The default media size is unchanged
(100 MiB); maximum configured size remains 512 MiB. Duration defaults to the
existing ten minutes. `MEDIA_MAX_VIDEO_SECONDS` can be explicitly reviewed up to
twelve hours without transcoding, but file size/storage/retention and bandwidth
must be assessed before increasing it. This does not permit arbitrarily large
YouTube API files merely because Google's API accepts them.

## Feature controls and remaining acceptance

New YouTube Publishing, Shorts and Standard Videos controls persist in the shared
revisioned feature table and default OFF, even when deployment ceilings allow
them. They require the exact channel/account allowlists and global publisher
gate. Thumbnails and native scheduling are unavailable controls, not claimed
functionality. Their inputs are rejected; `publishAt` is not silently translated
to the existing mock scheduler. No deletion, playlists, analytics, comment
moderation or edits are added. Custom thumbnails remain an optional follow-up.

Configuration/credential changes happen through existing protected Connections
forms with recent administrator login, origin and CSRF checks. Google secrets
are write-only. Local disconnect removes credentials and local access, preserving
publication/audit records. Provider avatars are served through a bounded,
authenticated, exact-Google-CDN proxy, not direct browser requests to Google.

Google OAuth and live read acceptance require the real channel ID, a configured
Google Web client and owner browser consent. Automated network tests are mocked;
no real upload or scheduled-release modification is authorized. Production
changes and live publishing each need separate applicable owner approval.

## Deployment and rollback

No schema migration is needed: provider configuration and pending encrypted PKCE
flows reuse SiteConfiguration; credentials/accounts/publications reuse existing
tables. New settings default off and do not modify Meta credentials or feature
preferences. The runtime requires no new packages; ARM64 uses the existing
prebuilt ffprobe. Google API credentials remain outside Git and image layers.

Before release:

1. Finish host/Docker regressions, real MCP HTTP mock tests, responsive browser
   checks, ARM64 build and offline wheel/dependency smoke. Record their exact
   source/image checksums in private operational evidence.
2. Obtain exact channel ID and review deployment settings. A connection-only
   acceptance can leave all upload ceilings off. To deliver usable administrator
   switches after a separately approved publishing rollout, set the supported
   YouTube deployment ceilings/account allowlist and initialize only the three
   new runtime preferences to false BEFORE raising those ceilings. Preserve all
   existing Meta flags and credentials. Do not reset the full feature row.
3. Back up the exact Compose/runtime/Nginx files and encrypted DB configuration;
   document root-private paths. Verify no active transfer/send and keep the old
   image. Update source via Git and only the SocialHub image/configuration.
4. Add the exact callback location to the existing site with query-free logging
   on every proxy hop. Keep the existing trusted forwarded headers. Validate the
   full Nginx config and gracefully reload only if separately authorized. No
   unrelated vhosts/Authelia roles/services/grants are needed.
5. After approved SocialHub-only restart, validate health, anonymous MCP 401,
   discovery, protected admin/forms, callback rejection and log privacy through
   the VPS. Then owner completes Google Login. Verify exact channel and existing
   video library using read-only tools; do not alter scheduled releases.
6. First live upload requires approval of a separate exact Private test asset,
   title/description/audience/disclosure. Verify resumable progress, processing
   and actual visibility. Public acceptance is separate and cannot be inferred
   from successful Private upload. Do not delete anything without authorization.

Rollback must stop new YouTube execution and wait for active transfer claims.
Back up and remove only the new `youtube_*` feature preferences before restoring
old application code (older versions reject unknown feature names). Preserve
other preference keys/revisions/audit history, credentials and all publication
rows; never rewind delivered evidence or requeue an uncertain target. Restore
backed-up Nginx/runtime/Compose and clean Git source through the normal guarded
release procedure, validate/reload and recreate only SocialHub. The separate
normalizer and Meta connections are unaffected. A root-private exact rollback
script must be prepared from the approved production baseline before rollout.

Hard deployment settings and callback/proxy changes require restart/reload;
feature preferences and saved Google credentials take effect without container
restart. Do not deploy automatically at the end of development.
