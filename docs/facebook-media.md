# Facebook images and videos (Phase 3D)

The implementation is feature-gated. Production text publishing and the existing
`chatgpt_direct` policy do not require media activation. Instagram remains outside
the authorized production write allowlist. Real image/video publication requires
separate operator authorization and controlled acceptance tests.

## ChatGPT attachment transfer: verify before activation

OpenAI documents explicit tool file inputs using `_meta["openai/fileParams"]`.
Each file input declares `download_url`, `file_id`, and optional `mime_type` and
`file_name` string properties. Only the first two are required. ChatGPT supplies
the temporary reference; SocialHub must not guess it from a sandbox path, image
shown to the model, or filename. See the [official file API reference](https://developers.openai.com/plugins/reference).

`probe_media_attachment(file)` tests this contract in the actual connected host.
When downloading is disabled or its exact host is unverified, it reports
`mode: reference_only`, the hostname and `download_verified: false`. That result
proves parameter delivery only. With an explicitly allowed host and probe flag,
it streams, validates and hashes temporary bytes, then removes them without
registering a media asset or publishing. Success is
`mode: validated_temporary_transfer` and `download_verified: true`.

Image and MP4 inputs must be tested separately, including a real Flow export
before claiming compatibility. **Documentation, a synthetic HTTP transport, or a
reference-only probe is not actual ChatGPT media ingestion acceptance.** Actual
host checks remain a rollout gate. Actual ChatGPT PNG and H.264/AAC MP4 attachment
probes have passed in isolated WSL staging: temporary download and validation
succeeded without registering or publishing media. This establishes the
pull-reference mechanism for these tested inputs. Actual real-account
publication preparation, a confirmed Flow export and authorized live media
publishing remain separate acceptance checks. Private operational notes hold
the regional host inventory and test evidence. If the host cannot provide an MP4 reference,
report the limitation and evaluate an inline file picker/upload bridge; do not
silently require pasted URLs, base64 payloads, a terminal or a separate portal.

Controlled ChatGPT PNG and MP4 imports and same-key reuse have also passed in
isolated staging. Server checks confirmed matching file hashes, private
permissions, future expiry and one import audit/idempotency record per asset.
No publication was prepared or executed in those tests. See the
[validation report](facebook-media-validation.md) for resource measurements and
the remaining rollout gates.

## Intended normal workflow

1. Attach an image or MP4 in ChatGPT and explicitly request a Facebook preview.
2. `import_media(file, idempotency_key)` imports the real host-provided reference.
   It returns a private media ID, checksum, type, dimensions/duration and expiry.
3. `prepare_publication` uses `account_ids: ["pixelpapa-facebook"]`, the caption,
   `content_type: image` or `video`, and that single media ID. The immutable preview
   includes validated metadata and content hashes.
4. Review the exact asset/caption/target and explicitly instruct publication.
5. `execute_publication` uses the existing authenticated policy and gates. Poll
   `get_publication_status` for a processing video; do not upload it again.

Importing or preparing never publishes. Scope, publisher role, exact client/user,
account restrictions, feature flags, hashes, idempotency and audit still apply.
Import keys are bound to the authenticated owner and file ID, not an expiring URL.
Repeated import returns the original available asset without fetching again;
using that key for a different attachment is rejected. URLs are not retained in
media records, idempotency responses or audit logs.

## Formats and resource bounds

- Images: fully decoded JPEG/PNG, at most 16 million pixels by default. The existing
  Facebook image-publishing subset is limited to 4 MiB; larger validated images
  are refused at publication preparation. No resizing/re-encoding is performed.
- Videos: MP4 containing one H.264 video stream and optional AAC audio, up to ten
  minutes, 4096 pixels per dimension and the configured byte cap. Unsupported
  Flow export codecs produce an actionable validation error; they are not
  silently transcoded.
- Default transfer cap: 100 MiB. Total temporary storage: 512 MiB / 16 files,
  with a 64 MiB free-space margin. One receiving/validating transfer across
  processes sharing the storage root; admission happens before file allocation.
- Whole-transfer deadline: 120 seconds, with separate connection/read deadlines.
  Streaming byte counts enforce the cap even without Content-Length.
- ffprobe is a pinned prebuilt ARM64-compatible runtime binary. Metadata only:
  file protocol, forced MOV/MP4 demuxing, external references disabled, one thread,
  16 MiB maximum allocation, bounded probe/analyze sizes, 20-second wall deadline,
  and a worker with 128 MiB data, ten CPU seconds and 128 KiB output limits.
  It inherits no Meta credentials. No FFmpeg encoder or encoding test dependency
  is installed or invoked. Normal tests use pre-generated fixtures.

## Temporary storage and cleanup

Use a dedicated private directory on the operator-approved HDD, bind-mounted at
`/data/media`, not the container's writable layer or its small `/tmp`. Container
UID/GID 10001 owns the directory (0700); generated UUID files are 0600. Caller
filenames never become filesystem paths. Staging files and final assets share
that filesystem, so validated uploads are atomically moved rather than copied.
The long-form bind must use `create_host_path: false`; verify the backing HDD
mount before startup so an absent disk does not create a new directory on the
system drive. Private deployment notes contain the actual host path.

Bytes expire after 24 hours by default (configurable from one to 48 hours).
Execution reserves at least an hour for delivery before minting a signed fetch
URL. A live sending lease prevents deletion until it ends. Expired bytes and
interrupted/orphan staging files are removed automatically; referenced hashes
and publication/audit metadata remain. Retention state never changes the
immutable preview hash. Expired bytes cannot be prepared or published until the
original file is imported again. A retained marker keeps cleanup running after
upload features are disabled. The timer runs at ASGI application lifetime, not
at the short stateless MCP request lifetime.

## Network and delivery security

Only exact operator-reviewed download hosts are allowed (empty by default).
ChatGPT may return different regional storage hosts for different attachments.
Review each hostname observed in an authenticated probe and add it individually
to `MEDIA_DOWNLOAD_HOSTS`; never allow `*.blob.core.windows.net` or infer trust
from an Azure suffix. DNS and TLS checks establish the network destination, not
proof that a file was issued by ChatGPT. Treat every file reference and its bytes
as untrusted, even on an allowed host. Keep the observed host inventory and
acceptance evidence in ignored operational notes, rather than public defaults.
The receiver requires HTTPS without credentials, IP literals or fragments,
rejects private/reserved/multicast DNS answers and pins a checked public address
while retaining hostname/SNI certificate verification. Environment proxies,
redirects, cookies, forwarded OAuth credentials and compressed transfer encodings
are not accepted. Actual bytes determine media type; MIME/name hints are not
trusted. Capability URLs and malformed-input values are excluded from errors.

Facebook fetches a checksum-bound, purpose-separated HMAC URL with a maximum
15-minute lifetime. The file route supports bounded single ranges and HEAD,
with private/no-store/nosniff headers. Keep access/query logging disabled at every
proxy hop; disable buffering and proxy temporary files for media. The normal
ChatGPT pull workflow does not need a publicly exposed raw upload endpoint.
Keep `/media/upload` blocked at the production edge; the retained administrative
HTTP importer has the same admission, quota and deadline controls.

## Asynchronous videos and uncertain results

A video POST returns a durable `video_processing` checkpoint. Status reads poll
only the acknowledged remote ID with GET, observing retry time and backoff.
A ready video becomes succeeded with its permalink. Explicit processing failure
is terminal; missing metadata/auth/rate-limit errors retain the checkpoint.
Read polling does not increment write attempts or require source bytes after
upload acknowledgement. It works with publishing subsequently disabled.

Repeating execution of an acknowledged upload delegates to reads rather than
claiming another write or issuing another upload URL. Unknown POST outcomes
still stop for verified reconciliation; no replacement upload, new publication
or new idempotency key is generated automatically. See [delivery finalization](delivery-finalization.md).

## Activation and rollback

All new ingestion/probe/media-write flags default off. The production templates
also keep signed delivery off. Activation requires real host file-transfer checks,
a tested HDD mount/permissions/quota, private proxy logging, signed external GET/
HEAD/range tests and explicit authorization for image/video publishing. Existing
Facebook text flags and `chatgpt_direct` settings remain separate.

Use the optional [media Compose fragment](../deployment/production/media.override.yml.example)
with an explicitly configured private host directory. Candidate code/fixtures
can be tested without publishing. Do not enable media gates just to demonstrate
that a tool exists.

To pause media with the new image, disable Facebook media publishing and new
ingestion, retaining issued signed downloads for outstanding accepted uploads
until their short lifetime ends. A rollback to older images which lack the new
media gate must disable BOTH global Meta/publishing switches first; that also
pauses text temporarily. Preserve the HDD mount, encryption key, immutable
requests and audit. Never recreate a remote post or restore an old live DB as
part of rollback. Private handoff notes contain exact affected paths, applied
versus proposed changes and rollback commands.
