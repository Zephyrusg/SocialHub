# Instagram Reel compatibility and optional normalization

The current Reel validator deliberately rejects MP4 edit lists and AAC bitrate
above 128,000 bps for Instagram. A Flow export that works on Facebook can therefore
fail Instagram preparation. Keep these platform-specific checks: changing a
deployment toggle does not change the format accepted by Meta.

Meta's [Instagram API documentation](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api?entity=request-23987686-3fe78620-2258-44b6-893f-42d76c7200d7)
specifies AAC audio at 128 kbps. The service retains its existing, narrower video
subset. FFmpeg's [MP4 muxer documentation](https://ffmpeg.org/ffmpeg-formats.html#MOV_002fMPEG_002d4_002fISOMBFF-muxers)
describes fast-start and edit-list controls. No validation limit is relaxed here.

## Available now: explicit WSL conversion helper

`scripts/normalize_instagram_reel.py` creates a **new local file**. It does not
connect to Meta, import media, prepare a publication, change a feature flag, or
publish. It requires Docker, the project Python environment, and prebuilt ffprobe
on PATH. Run it in WSL, not on the Raspberry Pi production host.

From the repository root, with the project's virtual environment activated:

```sh
# Pull explicitly once; the helper itself never downloads a tool image.
docker pull mwader/static-ffmpeg:9.0.2-1@sha256:f5d100184884eb3e82b0eb14bb5ebf98da99826ac824989720297fb9ef21c822
mkdir -p private/instagram-normalization
chmod 700 private/instagram-normalization
PYTHONPATH=. python scripts/normalize_instagram_reel.py \
  private/instagram-normalization/source.mp4 \
  private/instagram-normalization/instagram-compatible.mp4
```

Put the existing prebuilt ffprobe on PATH if necessary. Source and output must be
local paths, with a new output name and an existing output directory. Never place
real media, output reports or operational evidence in tracked directories.

The helper snapshots the input in a private scratch directory, stream-copies H.264
video, re-encodes only AAC audio at a **120 kbps target** and remuxes with
`-movflags +faststart -use_editlist 0`. The small audio bitrate headroom avoids
rejecting an encoder's nominal 128 kbps output when its measured average exceeds
128,000 bps. Audio re-encoding is lossy; video is not re-encoded.

It runs the pinned prebuilt FFmpeg image without network access, as the invoking
non-root user, with a read-only container root, capabilities dropped, one CPU,
384 MiB RAM, 64 processes, a 100 MiB output limit and a two-minute timeout. Only a
read-only input snapshot and a dedicated output directory are mounted. Timed-out
containers are removed. No FFmpeg source compilation or encoder installation in
the main SocialHub runtime is required.

Before accepting output, it runs the **existing Instagram Reel validator**, checks
dimensions/frame rate/duration, and compares every compressed H.264 packet hash
with the source. Failure removes temporary files and does not replace an existing
destination. Reports contain source/output hashes and metadata, not credentials.

MP4 without edit lists can expose B-frame delay and AAC encoder priming. For the
first real Flow export, all 240 video packets were unchanged; container duration
changed from 10.000 to 10.083333 seconds. Review playback before publication,
especially for music synchronized to visuals. Files requiring cropping, frame-rate
conversion, video re-encoding or substantial timeline repair are outside this
helper's scope and fail validation rather than being silently altered.

Attach the accepted derivative in ChatGPT and import it with a **new idempotency
key**. It must receive a new media object/hash and a new exact Instagram preview.
Reuse the desired caption explicitly. Never replace the bytes behind an existing
media ID, alter an immutable publication request, or retry a published Facebook
target to apply this correction. Importing the derivative does not authorize a
publication; the normal explicit publication instruction is still required.

Local scratch is removed automatically. Accepted output is retained for the user
to retrieve; remove it and the local source when no longer needed. Once imported,
normal SocialHub media ownership, expiry, quota and cleanup rules apply.

## Integrated ChatGPT workflow

`normalize_media(media_id, idempotency_key)` is an explicit MCP write tool that
creates private media, not a social post. It requires the existing publisher
identity, `socialhub:publish` scope, **Media Import** and **Instagram Media
Normalization**. Normalization defaults off even when its deployment ceiling
permits it. The administrator can enable it without restarting SocialHub.

1. Import the attachment with `import_media`.
2. Ask for an Instagram-compatible copy using `normalize_media` with the owned
   media ID and a stable new conversion idempotency key.
3. Review the returned new media ID, metadata and normalization disclosure.
   Already-compatible assets are reused without encoding; converted assets get
   new bytes/hash and normal ownership, expiry, quotas and cleanup.
4. Use the returned media ID in a **new** `prepare_publication` request. Keep the
   requested caption and explicit AI-disclosure choice. Never edit an existing
   immutable preview or reuse execution to fix an old Facebook publication.
5. Review and explicitly request publication through the existing policy.

The converter never calls Meta, creates a publication, changes a caption, grants
publishing permission or relaxes format validation. Repeated calls with the same
owner/key/source/profile reuse the stored derivative. Different input under the
same key is rejected. Expired sources/results must be imported again; conversion
does not silently extend their lifetime. Feature disabling blocks new jobs and
prevents an in-flight result from being registered after conversion completes.

An optional, separate `normalizer` container performs AAC encoding and MP4 remuxing.
The main runtime remains ffprobe-only. The worker has no DB/Meta credentials, no
mount of the private media store and no published host ports. Only SocialHub and
the worker join its internal Docker network. Worker scratch uses its own private
HDD directory. No public temporary URL or additional Nginx route is introduced.

Each transfer has a short-lived HMAC grant bound to a random job, source hash,
size and fixed profile. Persisted consumed-job markers prevent replay across
worker restarts. Returned bytes, hash, length and response signature are verified;
the main service independently validates Instagram metadata and compressed-video
identity before accepting the derivative. No redirects, proxy-environment use,
user-selected worker URL, file path, FFmpeg arguments or automatic HTTP retries
are allowed. Errors expose no grants, media bytes or raw worker diagnostics.

Jobs are serial, with 100 MiB input/output caps, bounded socket/encoding time,
one encoding thread, a 256 MiB worker container and a 0.5 CPU ceiling. The existing
cross-process media lock/quota protects the main service. Input/output scratch is
deleted on success, failure and disconnect. Worker cleanup removes crash remnants
and expired replay markers periodically; graceful conversion timeouts kill FFmpeg.
Force-killing the container kills its child processes as well.

## Verification and deployment boundary

Normal automated tests use existing tiny pre-generated MP4 fixtures and mocked
metadata. They never execute FFmpeg encoding or require the encoder image.
Regression coverage checks the actual failure properties, original immutability,
no-clobber behavior, container restrictions and compressed-video preservation.
The real source and exact results remain in ignored `private/` evidence.

The local helper alone requires no deployment. The integrated MCP step requires
both the updated SocialHub image and the optional worker; see the deployment and
rollback procedure below. No live Instagram publication is implied by conversion
testing. Accepted derivatives require their own reviewed publication instruction.

## Deployment and rollback

1. Build/test the main runtime normally and build the worker from
   `normalizer/Dockerfile`, using the repository root as context. Prepare and
   verify ARM64 artifacts before transferring them. No FFmpeg source compilation
   or encoder installation in the main runtime is necessary.
2. Back up the current Compose/runtime configuration, image/source revisions,
   encrypted feature row and effective feature state. Check active sends and
   preserve all existing account permissions, flags, media and credentials.
3. Generate a dedicated random secret locally into a root-readable-only env file
   (at least 64 characters). Give that file only to SocialHub and its worker; do
   not give the worker the application's full env file. Never commit this secret.
4. Provision a dedicated HDD scratch directory, owned by UID/GID 10001, mode 0700.
   Adapt `deployment/production/compose.normalizer.example.yml` to the existing
   bundle, retaining all existing application networks and env files. It is an
   overlay example, not a replacement for the homelab's production configuration.
5. Initialize `media_normalization=false` in the encrypted feature preferences
   before raising the new deployment ceiling. Older images reject unknown feature
   keys: do this while the old app is stopped, or after installing the new code
   with its ceiling still off. Preserve all other values and log the initialization.
6. Validate Compose, then start only the new worker and recreate only SocialHub.
   No Nginx, Authelia, database or unrelated-container restart is required. Verify
   health, private networking, no published ports, runtime dependencies and
   unchanged effective publishing preferences. The new toggle must be available
   but off. The owner can then enable it and refresh ChatGPT's tool list.
7. Perform a conversion-only smoke test with explicit owner activation; verify
   reuse, quota, temporary-file cleanup and a new preview. Do not publish content.

For rollback, stop SocialHub and the worker, restore the saved main image and
Compose/runtime configuration, and remove **only** the new `media_normalization`
preference before starting an older image. Preserve any newer changes to other
preferences; audit that compatibility adjustment. If no concurrent changes were
made, the protected encrypted feature-row backup can instead be restored exactly.
Do not roll back the database or erase existing originals/publications. Remove
the worker network/scratch only when unused. The new secret can be removed after
both containers no longer reference it. Record actual paths, timestamps and
validation results in ignored private homelab change tracking, not public Git.
