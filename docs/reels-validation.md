# Reel development, rollout and Facebook acceptance — 28 October 2026

## Results

- Current WSL suite: **478 passed**, 19.58 seconds.
- Current Docker suite: **478 passed**, 42.91 seconds, no network, read-only
  root, nonroot runtime user, dropped capabilities, 384 MiB RAM / half a CPU.
  Both runs retain three existing Alembic configuration deprecation warnings.
- ARM64 and AMD64 runtime builds completed using the existing uv multi-stage
  Dockerfile and pinned prebuilt ffprobe. No source compilation or new dependency.
- Local emulated ARM64 smoke check matched all 63 public application/script source
  files, confirmed default-off publication gates and verified runtime dependency
  exclusions. A subsequent native ARM64 offline validation also passed on the
  production host before switching the application image.
- Sanitized production Compose/Nginx template checks passed locally.
- GET-only production account assessment verified the expected Facebook identity,
  linked Instagram identity/username and Instagram publishing-limit access
  (usage zero). Stored scopes/Page tasks permit publication; the professional
  account type is separately identified as configured Creator.
- The explicitly approved code-only rollout recreated only SocialHub. Runtime
  environment bytes, owner policy, credentials, publication history and database
  schema were preserved. Unrelated running services were unchanged. Reel and
  Instagram publishing remain disabled; existing Facebook text policy is retained.
- External TLS, anonymous MCP authentication challenge, exact resource/issuer
  metadata, OIDC discovery and JWKS passed through the actual external proxy route.
- The owner reported a successful authenticated ChatGPT production status and
  Facebook capabilities check: database OK, `live_guarded_publishing`, existing
  Facebook writes allowed, and image/video/Reel capability gates all false.
  General live writes are true because the existing Facebook text workflow remains
  enabled. Only read-only tools were called.
- Deployment verification used GETs only. The subsequent explicitly authorized
  Facebook Reel publication succeeded, was independently verified through Meta,
  and was separately deleted at the owner's request. No replacement or additional
  publication was created. See [live acceptance](facebook-reel-live.md).

## Facebook export validation follow-up

After the read-only check, the owner authorized Facebook-Reel-only test activation.
Server capability checks confirmed Reel=true, image=false, video=false and
Instagram=false; only SocialHub restarted and policy/credentials/history stayed
unchanged. No publication was prepared or executed during activation.

A metadata-only check of a retained Flow export then exposed a validation bug:
Instagram's edit-list prohibition and 128 kbps audio ceiling were also applied to
Facebook. Meta's Facebook sample recommends AAC at 128 kbps or greater; it does
not impose those Instagram-specific restrictions. The development fix accepts
ordinary Facebook MP4 edit lists and nominal AAC rates above 128 kbps while
retaining all Instagram rules and the common codec, size, geometry, frame-rate
and duration checks. Three regression cases bring the source suite to
**460 passing tests in WSL and 460 in isolated Docker** (17.56 and 37.31 seconds).
The ARM64 patch image also passed the public-source/default-gate/dependency smoke
check locally. The observed Flow metadata now passes Facebook validation
offline and still fails Instagram's stricter subset. No video bytes are changed,
remuxed or transcoded. The owner approved patch deployment, and it is now
installed through Git with
a SocialHub-only restart. Native source/metadata checks and a read-only validation
of the actual retained Flow bytes pass. Facebook-Reel-only test activation is
preserved; Instagram, images and regular videos remain disabled. Runtime settings,
credentials, history, approval policy and schema are unchanged. No upload session
or publication was created.

The previous suite had 409 tests. The first Reel release added 48 cases; the
Facebook-specific correction adds three more, for 460 total. Coverage includes
both binary upload contracts, strict session URI matching, format/metadata bounds,
combined previews, independent results, permission failure, ambiguous uploads,
ambiguous finish/publication, delayed permalinks, approval expiry, disabled-gate
polling, and ordinary-video compatibility. Existing Facebook text/image/video
regressions continue to pass. Provider responses in these publishing tests are
mocked; they do not establish live Reel acceptance.

## First live upload: finish-order correction

The first native Facebook Reel session and binary upload succeeded. Meta confirmed
all bytes received with `video_status=upload_complete`; both processing and
publishing remained `not_started`. SocialHub incorrectly waited for processing
completion before allowing finish, causing repeated status polls without progress.
Meta's official collection explains that finish starts assembling/encoding.

The correction makes upload completion sufficient for Facebook's final execute
step. GET-only status checks expose readiness but never finish or resend an
upload. An ambiguous previous finish remains guarded against repetition, even
when Meta still reports processing not started. Instagram's FINISHED-before-publish
sequence remains unchanged. Mocks now reproduce Facebook's actual phase order,
and two additional regressions bring both WSL and isolated Docker suites to
**462 passing tests**. The ARM64 patch source/default-gate/dependency check passed
locally as well; no new package or ffprobe build is introduced.

The owner approved and deployed the correction with caller polling paused.
Native ARM64 protocol checks passed. The same publication/video IDs, immutable
hash and two saved init/upload attempts were preserved; environment, approval
policy, credentials, schema and unrelated services stayed unchanged. A live
GET-only interpretation now reports readiness for finish without modifying the
checkpoint or publishing; the existing approval was still valid at verification.
The owner resumes through the same authenticated plugin and publication ID.
Never create a replacement or repeat its upload. The owner subsequently resumed
this same publication successfully: Meta confirmed public delivery, final history
was succeeded/delivered and the mutation count remained three. Authorized cleanup
then removed the remote test object while preserving this history.

## Runtime footprint after the finish-order patch

| Measurement | Result |
| --- | --- |
| ARM64 compressed layer payload | 123,402,480 bytes / 117.7 MiB |
| ARM64 uncompressed layer tar payload | 342,823,936 bytes / 326.9 MiB |
| ARM64 exported gzip archive | 122,668,033 bytes / 117.0 MiB |
| Production host Docker-reported image size | 123,413,392 bytes / 117.7 MiB |
| WSL Docker/containerd reported resident image storage after unpacking | 480,940,944 bytes / 458.7 MiB; includes storage accounting beyond compressed content |
| Idle application RSS, initial disposable WSL AMD64 runtime | 94.9 MiB |
| Idle container working-set memory / CPU sample | 73.21 MiB / 0.14% |
| Metadata validation wall time, synthetic 5.02-second MP4 | 0.1166 seconds |
| Validation worker CPU time | 0.0356 CPU-seconds; about 30.5% of one core over that wall interval |
| Worker lifecycle peak RSS (`RUSAGE_CHILDREN`) | 60.7 MiB; includes inherited parent/startup pages |
| Native ARM64 application RSS after patch restart | 86.2 MiB; excludes PID-1 init wrapper |
| Native ARM64 metadata validation wall / worker CPU time | 0.3193 seconds / 0.1513 CPU-seconds, about 47.4% of one core |
| Native worker lifecycle peak RSS (`RUSAGE_CHILDREN`) | 71.2 MiB; includes inherited parent/startup pages |
| Runtime media/test executables/packages | ffprobe present; ffmpeg, pytest, Playwright and PyYAML absent |

The image size metrics describe different representations; do not treat the
archive size as installed disk usage. RSS and container working-set accounting
also differ and must not be added together. Worker RSS is not an incremental
physical allocation to add to server RSS. The measured fixture is synthetic,
720×1280 H.264/AAC at 25 fps, 10,412 bytes, fast-start without edit lists. It is
not a large-file or worst-case memory benchmark.

The resource run used only a disposable WSL container with synthetic SQLite data,
private tmpfs and no ports/network. Readiness and validation passed and the
container was removed. The approved rollout additionally used one disposable native
ARM64 validation container, without network, credentials or database, to inspect
the same synthetic fixture and validate both platforms' metadata rules. It was
removed afterward. Application RSS was measured from the actual Python server,
excluding its small PID-1 init wrapper. Cold samples do not predict every warmed-up
OAuth/media workload. The existing 384 MiB, half-CPU and 128-PID limits, read-only
root, dropped capabilities, private HDD media mount and lack of backend published
ports were verified unchanged.

No encoder or test-only media dependency was added to production. The inherited
static ffprobe still contains its vendor's broad linked feature/library set;
this release does not reduce or independently clear that native binary's prior
security/provenance concern. Do not describe it as a newly patched minimal build.

## Remaining work and authorization boundaries

1. [Feature Flag Management UI](feature-flag-management-plan.md) on the existing
   Settings page is next, before Instagram live work. It is proposed, not implemented.
2. Separately approved Instagram write-account allowlist expansion if its previews
   are needed; its Reel gate remains off until separately authorized.
3. Explicit owner instructions and reviewed previews for each new publication;
   separate Instagram/combined activation and live tests. This successful Facebook
   test is not authorization for additional posts.
4. Actual Instagram resumable-session eligibility for the existing app/login setup;
   GET-only permission evidence cannot prove this. Fail closed if refused.

See [workflow/protocol details](reels.md) and [deployment/rollback](reels-deployment.md).
Exact source/image checksums, private paths and operational evidence remain in
ignored `private/`. The code and Facebook export-validation patch are deployed;
the temporary Facebook Reel gate is disabled again. Facebook live acceptance
passed; Instagram and combined live acceptance remain pending. Runtime code stayed
unchanged during the test/documentation follow-up.
