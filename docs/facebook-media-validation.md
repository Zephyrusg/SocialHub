# Phase 3D validation — 2026-10-28

The feature-gated implementation passes 370 automated tests. Actual ChatGPT
attachment transfer, validation, retained import and idempotent reuse have passed
for a PNG and an H.264/AAC MP4 in isolated OAuth-authenticated WSL staging.
Production media publishing has not been activated or tested.

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
This is a deployment candidate size, not a claim that production was upgraded.
Private operational evidence contains the complete measurements and artifact
manifest, without committing identities, attachment references or credentials.

## Remaining gates

1. Confirm whether the tested clip is a Google Flow export before claiming that
   specific export path has passed.
2. Review a production candidate rollout with the approved HDD bind and importing,
   preserving existing Facebook text settings and keeping media delivery and
   Facebook media publishing disabled.
3. Test actual immutable image/video previews against the connected real account.
   Isolated staging mock accounts intentionally cannot use the durable publisher;
   this safeguard has not been bypassed.
4. Review and authorize the narrow signed media GET/HEAD route, verify external
   signed downloads and ranges, and verify expired/tampered URL rejection.
5. Obtain explicit authorization for exact live Facebook image and video test
   publications, one at a time. Instagram remains disabled. No real media was
   published in this phase's completed checks.
