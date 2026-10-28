# First live Facebook Reel acceptance

The first owner-authorized native Facebook Reel test passed on 28 October 2026.
This validates one tested MP4/profile and Page, not every export or Meta account.
Instagram and cross-platform live acceptance remain separate and disabled.

## Verified workflow

1. ChatGPT transferred a real MP4 attachment through the existing authenticated
   import tool. Validation and immutable preview creation succeeded.
2. The owner reviewed a Facebook-only Reel with an empty caption and explicitly
   instructed publication under the configured `chatgpt_direct` policy.
3. Native session creation and local binary upload succeeded. Meta confirmed all
   bytes received. A workflow defect initially waited for processing before finish;
   Facebook requires finish to start assembling/encoding. The correction shipped
   through Git and the reviewed ARM64 image while caller polling was paused.
4. The **same** publication and remote video were resumed. GET-only status exposed
   `fb_reel_ready` / `execute_next_step`. One final execute advanced to
   `fb_reel_publishing` / `poll_status`.
5. A subsequent GET-only status check recorded `succeeded`, `delivered`,
   `delivery_confirmed=true`, `next_action=none`, no error and no retry time.
   Total mutation steps: **three** — session start, binary upload and finish.
6. Independent administrative GETs verified the expected Page owns the exact video,
   `published=true`, ready video status, complete publishing phase, empty caption
   and the same confirmed permalink. Cached delivered reads made no Meta request.

No replacement publication, second upload or new import was created to recover
the interrupted workflow. The immutable request/content hash and remote ID survived
the deployment. Exact publication/media/remote IDs, hashes, permalink and sanitized
operational evidence are recorded in ignored `private/`, as required by the
repository's separation of public source from personal test records.

## Authorized cleanup and final gates

After acceptance, the temporary Facebook media gate was disabled, preserving the
current application image and existing Facebook text/owner policy. Only SocialHub
restarted; unrelated services were unchanged. Instagram remained disabled.

The owner separately authorized deleting the exact test Reel after verification.
A narrow administrative cleanup recorded an audit event before sending **one**
DELETE. Meta acknowledged success; a GET verified the node was no longer readable
while the Page token still worked. No deletion retry, replacement or new post was
performed. General MCP deletion permission was not enabled.

Publication history deliberately remains succeeded/delivery-confirmed, with the
original three mutation attempts, immutable hash and pre-deletion permalink. A
separate `cleanup.state=deleted` marker records removal. Cached status does not
query the removed object, downgrade history or publish it again. The saved link
is historical and is not a claim that deleted content remains accessible.

## Automated follow-up

**478 tests pass in WSL and isolated Docker.** The runtime code did not change
during this follow-up; added tests and documentation do not require a new
production image.

| Scenario | Regression coverage |
| --- | --- |
| Exact missing step | Upload complete → `fb_reel_ready` → final execute → publishing → delivered |
| Process restart | Fresh coordinator/connectors at upload, processing, ready, publishing, delivered-metadata-pending and succeeded checkpoints |
| Ambiguous mutation/crash | Expired leases before/after provider acceptance for initialization, upload and finish; no blind resend |
| Concurrent/duplicate calls | Active send lease blocks another dispatch at each mutation boundary |
| Terminal replay | Repeated execute/status calls keep one remote post and three mutation steps |
| Deleted delivered object | Network access deliberately forbidden; repeated status/execute calls remain cached and cannot republish |
| MCP contract | Consequential execute annotations, same-request guidance, retry timing and uncertain-outcome instructions; status explicitly GET-only |

See [Reel workflow and next-action rules](reels.md),
[validation/resources](reels-validation.md), and [deployment/rollback](reels-deployment.md).

The next planned improvement is the [Feature Flag Management UI](feature-flag-management-plan.md)
on the existing Settings page, before Instagram live publishing work. This acceptance
report does not authorize additional Facebook publications or Instagram activation.
