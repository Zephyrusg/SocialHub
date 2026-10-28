# Reel development validation — 28 October 2026

## Results

- Complete WSL suite: **457 passed**, 17.47 seconds.
- Complete Docker suite: **457 passed**, 37.61 seconds, no network, read-only
  root, nonroot runtime user, dropped capabilities, 384 MiB RAM / half a CPU.
  Both runs retain three existing Alembic configuration deprecation warnings.
- ARM64 and AMD64 runtime builds completed using the existing uv multi-stage
  Dockerfile and pinned prebuilt ffprobe. No source compilation or new dependency.
- Local emulated ARM64 smoke check matched all 63 public application/script source
  files, confirmed default-off publication gates and verified runtime dependency
  exclusions. This is not a fresh native Raspberry Pi performance test.
- Sanitized production Compose/Nginx template checks passed locally.
- GET-only production account assessment verified the expected Facebook identity,
  linked Instagram identity/username and Instagram publishing-limit access
  (usage zero). Stored scopes/Page tasks permit publication; the professional
  account type is separately identified as configured Creator.
- No production files, settings or services changed. No upload session, Reel
  publication, edit or deletion was performed against Meta.

The previous suite had 409 tests. The expanded suite adds 48 cases, including
both binary upload contracts, strict session URI matching, format/metadata bounds,
combined previews, independent results, permission failure, ambiguous uploads,
ambiguous finish/publication, delayed permalinks, approval expiry, disabled-gate
polling, and ordinary-video compatibility. Existing Facebook text/image/video
regressions continue to pass. Provider responses in these publishing tests are
mocked; they do not establish live Reel acceptance.

## Runtime footprint

| Measurement | Result |
| --- | --- |
| ARM64 compressed layer payload | 123,373,660 bytes / 117.7 MiB |
| ARM64 uncompressed layer tar payload | 342,723,072 bytes / 326.8 MiB |
| ARM64 exported gzip archive | 122,643,372 bytes / 117.0 MiB |
| Docker/containerd reported resident image storage after unpacking | 480,800,963 bytes / 458.5 MiB; includes storage accounting beyond compressed content |
| Idle application RSS, disposable WSL AMD64 runtime | 94.9 MiB |
| Idle container working-set memory / CPU sample | 73.21 MiB / 0.14% |
| Metadata validation wall time, synthetic 5.02-second MP4 | 0.1166 seconds |
| Validation worker CPU time | 0.0356 CPU-seconds; about 30.5% of one core over that wall interval |
| Worker lifecycle peak RSS (`RUSAGE_CHILDREN`) | 60.7 MiB; includes inherited parent/startup pages |
| Runtime media/test executables/packages | ffprobe present; ffmpeg, pytest, Playwright and PyYAML absent |

The image size metrics describe different representations; do not treat the
archive size as installed disk usage. RSS and container working-set accounting
also differ and must not be added together. Worker RSS is not an incremental
physical allocation to add to server RSS. The measured fixture is synthetic,
720×1280 H.264/AAC at 25 fps, 10,412 bytes, fast-start without edit lists. It is
not a large-file or worst-case memory benchmark.

The resource run used only a disposable WSL container with synthetic SQLite data,
private tmpfs and no ports/network. Readiness and validation passed and the
container was removed. No production benchmark container was started.
Prior native Pi observations were roughly 72–93 MiB idle RSS; around
100 MiB remains a reasonable planning estimate, subject to a fresh approved native
check. The existing 384 MiB runtime limit is preserved.

No encoder or test-only media dependency was added to production. The inherited
static ffprobe still contains its vendor's broad linked feature/library set;
this release does not reduce or independently clear that native binary's prior
security/provenance concern. Do not describe it as a newly patched minimal build.

## Remaining gates

1. Approval for a code-only production rollout with Reel gates still disabled.
2. Fresh actual Flow MP4 import under the new metadata checks and preview review.
3. Separate authorization for Facebook-only, Instagram-only and combined live tests.
4. Actual Instagram resumable-session eligibility for the existing app/login setup;
   GET-only permission evidence cannot prove this. Fail closed if refused.
5. Native Pi validation/resource observations during an approved deployment window.

See [workflow/protocol details](reels.md) and [deployment/rollback](reels-deployment.md).
Exact source/image checksums, private paths and operational evidence remain in
ignored `private/`; the candidate has not been deployed.
