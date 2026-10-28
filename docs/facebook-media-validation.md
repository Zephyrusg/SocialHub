# Phase 3D validation — 2026-10-28

The feature-gated implementation passes 371 automated tests. Actual ChatGPT
attachment transfer, validation, retained import and idempotent reuse have passed
for a PNG and an H.264/AAC MP4 in isolated OAuth-authenticated WSL staging.
Production media publishing has not been activated or tested.

The owner subsequently authorized an import-only production rollout. The tested
ARM64 runtime is healthy with a private HDD media bind and bounded imports.
Existing Facebook text publishing and its approval policy were preserved;
Facebook media publishing and Instagram writes remain disabled. Signed delivery
was subsequently enabled for separately authorized download-only testing.
Credentials, publication history and the database schema were unchanged, and only
SocialHub was recreated. Public discovery/authentication checks passed through
the external proxy; raw upload remains blocked. A narrow UUID-only GET/HEAD media
route now requires valid per-asset, checksum-bound, short-lived signatures (or
authenticated owner access verified by the application).
Actual ChatGPT production image and Google Flow video imports and real-account
immutable previews have passed. Independent server checks confirmed matching
stored bytes/content hashes, private permissions, expiry and zero execution
attempts for both prepared requests. No publication was executed.

## Actual ChatGPT checks

| Check | Result |
| --- | --- |
| PNG reference, temporary download and validation | Passed, 1254 × 1254, 3,913,655 bytes |
| MP4 reference, temporary download and validation | Passed, 720 × 1280, 10.005 seconds, H.264/AAC, 3,966,770 bytes |
| Regional attachment hosts | Individually reviewed exact hosts; no Azure wildcard |
| Image import and same-key reuse | Passed; initial import independently confirmed from stored audit |
| MP4 fresh import then same-key reuse | Passed; `reused: false` then `true` |
| Server-side integrity, access permissions and expiry | Passed for both stored assets |
| Audit/idempotency persistence | One import audit and one idempotency record per tested asset |
| Staging publications | Zero |
| Production image import and immutable preview | Passed; prepared with zero execution attempts |
| Production Google Flow video import and immutable preview | Passed; prepared with zero execution attempts |
| External image/video signed GET, HEAD and prefix/suffix ranges | Passed through the VPS with verified TLS |
| External unsigned, expired, tampered, duplicate and cross-asset signatures | Rejected |
| External multiple/unsatisfiable ranges and write methods | Rejected; upload route remains blocked |

The owner confirmed that the tested 10.005-second H.264/AAC MP4 was a Google
Flow export. Its actual ChatGPT transfer, validation, staging import and reuse
passed. Its production import and preparation have also passed. This confirms
the tested export profile; real publishing remains a separate check.

The automated suite covers captions, immutable previews, ownership, quotas,
stream limits, cancellation, expiry cleanup, SSRF/DNS/TLS boundaries, exact-host
isolation, image finalization, asynchronous video readiness/failure and preventing
another upload after acknowledgement. No automated test publishes to Meta.

## ARM64 runtime measurements

The candidate was tested natively on a Raspberry Pi using an ephemeral container
with no external network or published ports, synthetic credentials, read-only
root filesystem, dropped capabilities, 384 MiB memory limit and half a CPU.
Fixtures and a synthetic SQLite database were placed on an authorized temporary
HDD directory. Existing containers were checked before and after: none restarted.
The temporary container and directory were removed afterward.

| Measurement | Observed result |
| --- | --- |
| Docker-reported ARM64 candidate image size | 123,394,345 bytes (117.7 MiB) |
| Sum of uncompressed layer tar sizes | 342,901,760 bytes (327.0 MiB), including layer metadata/overlays |
| Idle server RSS | 93.1 MiB |
| Idle server CPU over a two-second sample | 0% observed |
| Server peak RSS during image/video HTTP validation | 98.3 MiB |
| Bounded video validation worker wall time | 0.659 seconds |
| Worker CPU time | 0.240 CPU-seconds, about 36.4% of one CPU averaged over that interval |
| Worker lifecycle peak RSS (`RUSAGE_CHILDREN`) | 99.6 MiB; includes inherited fork/startup memory |
| Standalone ffprobe | Present, prebuilt and pinned |
| FFmpeg encoder executable | Absent |
| pytest, Playwright and PyYAML | Absent from candidate runtime |

The worker measurement includes its Python startup and metadata probing. Parent
and child RSS share pages and must not simply be added as physical RAM usage.
The tiny pre-generated fixture is a three-second, 5,622-byte MP4 without audio;
these numbers are a functional resource check, not a worst-case capacity claim.
The actual ChatGPT H.264/AAC clip was tested separately in WSL. No transcoding or
fixture encoding occurred during either check. No additional media dependencies
were installed for Phase 3D; the runtime retains the existing standalone ffprobe
and image validator. Test fixtures and benchmark harnesses are not in the image.

The candidate image contains application source from commit `05802ed`; subsequent
regional-host tests and acceptance-report commits do not change runtime code.
The same measured artifact now serves the authorized import-only production
rollout; image/video publishing acceptance is still pending.
Private operational evidence contains the complete measurements and artifact
manifest, without committing identities, attachment references or credentials.

## Remaining gates

The signed-download rollout passed the full production Nginx syntax check before
a graceful reload; only SocialHub was recreated for delivery settings. Thirty
external checks passed without Meta requests or publication calls. The privacy
marker was absent from Pi container logs; the operator confirmed a zero count
in the bounded VPS access/error log check. Capability URLs remained in memory
and were not printed or written into reports. VPS configuration was unchanged.
Post-test checks confirmed both media previews still prepared, zero execution
attempts, Facebook media publishing disabled and existing text policy preserved.

1. Obtain explicit authorization for exact live Facebook image and video test
   publications, one at a time. Instagram remains disabled. No real media was
   published in this phase's completed checks.
