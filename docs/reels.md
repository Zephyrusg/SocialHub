# Native Facebook and Instagram Reels

Reels reuse the existing imported media, immutable previews, approvals and
independently checkpointed publication targets. Development tests use mocked Meta
responses. **No live Reel acceptance test or production rollout has been performed.**
Facebook text, image and regular `content_type="video"` publishing remain separate.

## ChatGPT workflow

1. Attach an MP4 and request a Reel preview. `import_media` receives the actual
   ChatGPT file reference, validates it once and returns a private media ID/hash.
2. Call `prepare_publication` with `content_type="reel"`, that single media ID,
   the exact caption and one or both account IDs:
   `pixelpapa-facebook`, `pixelpapa-instagram`. The preview contains one shared
   asset and independent account targets. A combined request must meet both
   platforms' supported subsets.
3. Review the exact accounts, caption and media metadata/hash. Preparing a preview
   grants no publishing authorization. The owner must explicitly request publication.
4. `execute_publication` advances **at most one mutation per target per call**.
   Within that explicitly requested workflow, ChatGPT can continue the same
   publication ID through session creation, upload and final publication.
5. Respect `retry_at`. When `next_action="poll_status"`, use
   `get_publication_status`. Status reads only issue Meta GETs. Readiness returns
   `next_action="execute_next_step"`; finishing still requires an execute call
   under the original valid authorization. No scheduler publishes in the background.
6. Report each platform's state, remote ID, error and final permalink. A successful
   target is never resent while the other target continues. On
   `manual_reconciliation`, stop mutations and inspect the existing remote outcome.

One caption currently applies to both platforms. The immutable request and the
per-target connector request are separate, allowing a future reviewed caption map
to be hashed into the preview without replacing the target state machine. Such a
map is not implemented in this release.

## Upload protocols and checkpoints

| Platform | Session | Private binary transfer | Readiness | Publication | Confirmation |
| --- | --- | --- | --- | --- | --- |
| Facebook | POST `/{page}/video_reels`, `upload_phase=start` | POST `rupload.facebook.com/video-upload/{version}/{video_id}` | GET video `status`; uploading and processing phases complete | POST `/{page}/video_reels`, `upload_phase=finish`, `video_state=PUBLISHED` | GET publishing phase complete and `permalink_url` |
| Instagram | POST `/{ig}/media`, `media_type=REELS`, `upload_type=resumable`, caption | POST `rupload.facebook.com/ig-api-upload/{version}/{container_id}` | GET container `status_code=FINISHED` | POST `/{ig}/media_publish`, `creation_id` | Save returned media ID before GET `permalink` |

Facebook stages: `fb_reel_upload` → `fb_reel_processing` → `fb_reel_ready` →
`fb_reel_publishing` → `delivered`/succeeded. Instagram stages:
`ig_reel_upload` → `ig_reel_processing` → `ig_reel_ready` → `delivered`/succeeded.
Every acknowledged mutation is saved before the next mutation. Status-driven
stage transitions and finalization are audited without incrementing mutation
attempts. Attempts count durable mutation steps, not whole publications.

Both upload requests stream 64 KiB chunks from the locally opened, hash-checked
private file, using `offset: 0`, `file_size`, Content-Length and OAuth authorization
headers. They never send `file_url` or `video_url`. The exact returned upload URI
must match the configured Graph version, numeric remote ID, platform-specific
path and HTTPS `rupload.facebook.com` host. No redirects, environment proxies,
credentials in URLs, arbitrary upload endpoints or automatic upload retries.
The transfer has a 150-second overall deadline inside the existing five-minute
target lease. The existing import byte limit still applies.

Meta calls Instagram's mechanism resumable, but this implementation deliberately
does not guess byte offsets or resume an ambiguous transfer. After an upload
timeout, GET-only processing evidence may prove that the existing upload arrived.
Until then it stays uncertain. No replacement session or second upload is created.
A Facebook finish timeout can reconcile through its saved video ID and publishing
phase. An Instagram publish timeout followed by `PUBLISHED` without a saved media
ID remains uncertain and requires independent reconciliation; a container ID is
not presented as a published media ID. The server does not guess a permalink or
call `media_publish` again. There is no automatic recovery for a crash between
remote session creation and saving its response.

Instagram resumable eligibility depends on Meta app/login configuration, including
Facebook Login for Business support where required. The existing account's content
publishing permission does **not** independently prove that eligibility. A refused
session fails with an actionable message. There is no silent hosted-URL fallback.
Therefore Reels introduce no new public media route, signed link, private-directory
exposure, Nginx change or delivery dependency. Existing signed delivery remains
available separately for previously implemented Facebook image/video publishing.

## Supported media subset

The metadata-only ffprobe pipeline now retains frame rate, pixel format, scan
mode, video bitrate and audio sample rate/channels/bitrate. A bounded MP4 box walk
checks fast-start ordering and rejects edit lists for Reels. This does not decode
frames, transcode, re-encode or add an encoder to the runtime.

| Property | Facebook Reel | Instagram Reel |
| --- | --- | --- |
| Container/codecs | Fast-start MP4, H.264/yuv420p; optional AAC | Same supported subset |
| Shape | Exact 9:16; width 720–1080 | Same supported subset |
| Frame rate | 23–60 fps, progressive/unspecified scan metadata | Same supported subset |
| Duration | 4–60 seconds | 3–600 seconds (existing ingestion ceiling) |
| Video bitrate | At most 25 Mbps | At most 25 Mbps |
| Audio | At most 48 kHz, mono/stereo, at most 128 kbps | Same supported subset |
| Caption | At most 2200 characters | At most 2200 characters |
| Bytes | Existing configured upload limit; default 100 MiB | Same service limit, below Meta's documented 1 GB maximum |

These deliberately narrower service limits are not claims about every format Meta
accepts. Meta's samples document different duration bounds and broader formats.
Metadata cannot prove every codec bitstream property, such as closed GOP or all
platform processing/copyright requirements; Meta can still reject a validated file.
Unknown required metadata fails before session creation. Existing media imported
before these metadata fields were collected must be re-imported and prepared with
a new idempotency key. The same bytes can reuse their media ID after revalidation;
older frozen previews are not rewritten. Reject unsupported exports; do not
silently remux or transcode them.

Existing media ownership, exact attachment-host allowlisting, storage quotas,
private permissions, one-hour delivery reservation, retention and cleanup apply.
Files are required until upload acknowledgement. Later processing and finalization
do not require keeping local bytes indefinitely. There are no new runtime packages,
transcoding workers, public upload endpoints or persistent media copies.

## Read-only readiness and acceptance

`python scripts/verify_reels_readiness.py` verifies the expected Page/Instagram
link and username using GETs, reads Instagram's publishing quota, and separately
reports stored scopes/Page-task evidence. It never refreshes credentials, changes
the database or starts upload sessions. The professional account type is identified
as configured; this Facebook Login IGUser path does not independently return that
type. Resumable eligibility remains explicitly unverified until an authorized test.
Run it inside the reviewed application's environment; do not paste credentials.

The automated suite covers both protocols, local streamed bytes, URI pinning,
format limits, combined preview, independent final URLs, one-platform failure,
upload/finish ambiguity, approval expiry, read-only polling and replay protection.
The synthetic five-second H.264/AAC fixture is pre-generated; normal tests do not
encode media. See [deployment and rollback](reels-deployment.md).
The [development validation report](reels-validation.md) records test counts,
ARM64 artifact size, measured resources and remaining live acceptance gates.

Primary references checked on 28 October 2026:

- [Meta's Facebook sample and requirements](https://github.com/fbsamples/reels_publishing_apis/tree/main/fb_reels_publishing_api_sample).
- [Meta's Facebook API collection: local binary upload and finish](https://www.postman.com/meta/facebook/documentation/r56bjfd/facebook-api).
- [Meta's Instagram sample: resumable upload, processing and media requirements](https://github.com/fbsamples/reels_publishing_apis/tree/main/insta_reels_publishing_api_sample).
- [Instagram publishing guide](https://developers.facebook.com/docs/instagram-platform/content-publishing/) and [Facebook guide](https://developers.facebook.com/docs/video-api/guides/reels-publishing/).
  Developer guide retrieval was rate-limited during this review; the accessible
  official samples/collection establish the implemented contract. Controlled
  Graph v26.0 acceptance remains necessary before claiming live compatibility.
