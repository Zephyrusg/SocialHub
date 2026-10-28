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
containers are removed. No FFmpeg stack is compiled or added to production.

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

## Proposed ChatGPT workflow (not implemented or deployed)

Normalization should be optional and explicit rather than part of every import
or `prepare_publication` call. Recommended design:

1. Import once, inspect the target-specific incompatibilities and offer to create
   an Instagram-compatible derivative. Leave the original usable for Facebook.
2. Require the owner-controlled **Media normalization** feature flag, initially
   off, with deployment ceilings, authorization, revision checks and audit events.
   A flag must not be shown as operational until the worker is implemented.
3. Run a bounded job on a separate WSL/Gaming PC worker. Keep the Pi runtime
   ffprobe-only. If that worker is offline, return a clear unavailable status.
   A Pi-side optional worker is a later explicitly approved resource tradeoff.
4. Give workers only a short-lived grant for one owned source and one result.
   Enforce authenticated transfer, hashes, quotas, job expiry, concurrency limits,
   cancellation and failure cleanup. Do not mount the entire private media store.
5. Deduplicate by owner, source hash, normalization profile and profile version.
   Create a new owned media object with source/provenance metadata and its own TTL.
   Validate it independently on the server before allowing preparation.
6. Show the derivative's audio change, timing, metadata and hash in the preview.
   Preserve captions, AI disclosure, account restrictions and existing approval
   policy. Do not publish or modify a prepared request automatically.

This separates the lightweight production service from conversion tooling and
removes repeated manual preprocessing once the optional worker is implemented.
The current change provides the reproducible local correction and assessment;
it does **not** claim automatic normalization from ChatGPT is available yet.

## Verification and deployment boundary

Normal automated tests use existing tiny pre-generated MP4 fixtures and mocked
metadata. They never execute FFmpeg encoding or require the encoder image.
Regression coverage checks the actual failure properties, original immutability,
no-clobber behavior, container restrictions and compressed-video preservation.
The real source and exact results remain in ignored `private/` evidence.

No production deployment, restart, migration or feature-flag change is required
for this local helper. Production image size, idle RAM and ffprobe-only dependency
set are unchanged. No live Instagram publication has been tested by this fix.
Rollback is simply to stop using the helper and delete the local derivative;
existing accounts, imported originals, previews and Facebook posts are unaffected.
