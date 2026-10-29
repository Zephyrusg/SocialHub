# Instagram photos and Music Stories — Phase 3H.1

Implemented locally; no production deployment, account conversion, publication or
music availability verification was performed. This release adds no scheduler,
carousel, private Instagram interface or cover/audio video rendering.

## Capability investigation (October 29, 2026)

Meta's [official Instagram API collection](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api?entity=request-23987686-3a63181c-e5c6-4890-96dd-e4d425bef5e0)
describes Facebook Login publishing for professional Creator and Business accounts,
with API Stories restricted to Business accounts. PixelPapa remains a Creator.
Feed photos do not require conversion. The existing connection requires a linked
Facebook Page, `instagram_basic`, `instagram_content_publish`, Page discovery/read
permissions and the appropriate content-creation Page task. Existing server-side
permission checks and the account allowlist remain authoritative.

No supported API for attaching the native Story Music sticker or searching the
Instagram Music catalog was established. Meta's current [generated IGUser SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/iguser.py)
exposes image/container publishing, and audio-related creation parameters, but
those parameters are not evidence of Story Music support. Reels audio features
must not be repurposed as Story Music sticker support. Accordingly, this service
does not send a music parameter, claim to select a recording, or query a private
music endpoint. This conclusion is conservative: the primary developer guide and
reference pages returned access errors/HTTP 429 during research, so their latest
full text could not be independently read.

Meta's [Sharing to Stories guide](https://developers.facebook.com/docs/instagram-platform/sharing-to-stories/)
describes a mobile-app handoff concept: Android `ADD_TO_STORY` with a local
`content://` image URI and temporary read permission; iOS uses a custom URL scheme
and pasteboard image bytes. The official guide was inaccessible during this check;
these native mechanisms need fresh documentation/device validation before use.
SocialHub is a web/MCP service, with no native Android/iOS bridge. An HTTPS
image URL cannot substitute for a phone-local content URI. No fake intent link,
forced package selection, or native handoff control is exposed. The implemented
Web Share file chooser may offer Instagram depending on device/browser/app; it
cannot guarantee the Story composer or preselect music. Saving to Gallery is the
supported fallback. Desktop users open the private transfer URL on their phone.

The official [Meta Music sticker description](https://about.fb.com/news/2024/05/new-stickers-in-instagram-stories/)
places song selection inside Instagram. PixelPapa's exact recording availability
is **unverified**. DistroKid delivery is not proof of account/region searchability.
Manual check: in @pixelpapamusic's Story composer, tap Stickers → Music, search
artist **PixelPapa** and title **It's Dangerous to Go Alone**, verify both credits
and listen. Select the excerpt there. If absent, retry after the October 30, 2026
release and check delivery with the distributor; do not substitute another track.

## ChatGPT workflow

1. Import the user-supplied official cover using `import_media` (or authenticated
   media upload). Ownership, limits, decoding, retention and private storage apply.
2. Call `prepare_instagram_artwork` with `purpose=feed` and a unique idempotency key.
   Pillow decodes the owned PNG/JPEG, applies EXIF orientation, flattens transparency,
   strips metadata and creates JPEG at width 1080 with a supported aspect ratio.
   Padding preserves the complete cover. No AI image generation is involved.
3. Use the returned media ID in `prepare_publication` with account
   `pixelpapa-instagram`, `content_type=image` and the requested caption. Review the
   immutable stored caption, account and asset checksum. Obtain explicit approval
   through the existing approval policy, then execute that publication.
4. Continue the same approved workflow: create a container, wait/poll the saved
   processing checkpoint using `get_publication_status`, execute the ready step
   once, then resolve the acknowledged media ID/permalink with GETs. No additional
   manual Instagram action is required for feed publication. Respect `retry_at`.
5. Separately call `prepare_instagram_artwork` on the same original cover with
   `purpose=story`, a different key and optional `announcement` (up to 140 chars).
   The result is a 1080×1920 JPEG, centered uncropped artwork and optional text.
   The image includes no song audio or simulated Music sticker.
6. Open `transfer_url` on the phone. Save artwork, or try Share artwork when the
   browser supports files. Select the artwork in Instagram, add the official Music
   track and excerpt, review and publish there. A manual announcement can also be
   added inside Instagram.

Story results always report `status=artwork_prepared`, `published=false`,
`publication_mode=manual_in_instagram` and music availability unverified.
There is no Story publication target or success state. Sharing/downloading cannot
prove Instagram delivery. Feed and Story use separate statuses and approvals.

Artwork idempotency binds principal, source ID/hash, purpose, announcement and
render profile. Reusing a key refreshes 15-minute private links for the same
unexpired output; different inputs conflict. Expired bytes require a fresh import
and key. Source and derivative are separate validated assets. Storage admission,
cleanup and quotas cover rendering. Signed URLs are bearer capabilities: use the
returned link on the phone within 15 minutes; pages are non-cacheable, have no
external resources and send no referrer. Disabling Story artwork blocks new
preparation and transfer pages. Existing signed media links retain their bounded
lifetime under the existing Media Delivery switch.

## Durable photo safety and recovery

Each mutation occurs after a durable approval-bound claim. Photo checkpoints are
`ig_photo_processing`, `ig_photo_ready`, then `delivered` with the saved container
and published media IDs. Status tools use GET only and never call `media_publish`.
The container is checked again immediately before final publishing. Expired/error
containers fail without replacement. Timeouts during creation or final publishing
become uncertain and cannot be retried blindly. A creation timeout without a known
container requires operator investigation; it cannot prove a published photo.

For an uncertain final publish, the existing local `reconcile_publication.py`
command verifies a recorded finish attempt, the saved container's PUBLISHED state,
one unique account-owned FEED/IMAGE with exact caption and bounded execution time,
and a valid Instagram `/p/` permalink. Ambiguous candidates are rejected. This is
explicit owner-selected evidence; re-encoded remote image bytes are not claimed
to equal the local hash. No remote mutation occurs in reconciliation.

## Approved rollout procedure

Production deployment still requires explicit authorization. Before raising either
new ceiling, use the reviewed rollout artifact's helper against the intended DB:

```
python -m scripts.initialize_instagram_features --administrator EXISTING_ADMIN_SUBJECT
```

This uses the shared encrypted feature registry, revision/CAS, administrator
allowlist and audit log to persist `instagram_photos=false` and
`instagram_story_artwork=false`. It refuses already-on preferences. Verify both
are explicitly off. No schema migration is needed. Back up configuration under
the existing deployment procedure.

In the same approved rollout, set `INSTAGRAM_PHOTOS_ENABLED=true` and
`INSTAGRAM_STORY_ARTWORK_ENABLED=true` in the actual runtime environment and deploy
the reviewed code. The initial deployment/environment change requires the existing
SocialHub restart; document/approve it before rollout. Photo availability also
requires the existing global write ceiling, Instagram account allowlist, signed
media delivery and HTTPS public media base. Artwork requires media ingestion,
delivery and the public base, but not Business-account conversion or Story writes.
Ensure the reverse proxy forwards `/instagram/story/` as well as `/media/` to
SocialHub; do not add public file-system access.

After rollout, both implemented controls must be available but **off** in the
existing administrator Settings page, ready for its normal enable action without
another Codex intervention. Runtime changes require no restart. Photos additionally
obey the Instagram Publishing master switch and Media Delivery; Story artwork
obeys Media Import and Media Delivery. Turning off Photos prevents new container
creation and final publish dispatch, including prepared/approved work. An external
request already dispatched cannot be recalled. Account/role checks, CSRF/origin,
concurrency, publication approvals and deployment ceilings are unchanged.

No live provider mutation or device handoff has been tested by this development
work; perform an explicitly authorized photo smoke publication and manual phone
check as rollout acceptance, with separate publication approval.

## Development validation

Full regression suite: **654 passed, 2 skipped**. The skipped tests require an
explicitly configured disposable local MariaDB; shared registry/CAS SQLite tests
passed. Three existing Alembic deprecation warnings remain. New checks cover
Creator feed approval, durable checkpoints, ambiguous outcomes without resend,
container errors/expiry, verified photo reconciliation, disabling before dispatch,
parent dependencies and capability discovery, explicit-off rollout initialization, derivative ownership and
idempotency, transfer signatures/downloads, MCP discovery and manual-only Story
status. `git diff --check` and Python compilation also passed. Mocked checks do
not establish live Meta acceptance, phone share targets or Music Library presence.
