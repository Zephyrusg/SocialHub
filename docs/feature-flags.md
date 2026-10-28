# Feature Flags in Settings

The existing `/admin/` Settings/setup page includes server-rendered switches for
runtime preferences. Use your existing, explicitly allowlisted administrator
identity and recent MFA. No new identity, OAuth scope or approval portal is needed.
Administrator permission does not grant publishing permission, and membership of
an unrelated Authelia admin group does not grant SocialHub access.

## Configured, available and effective

- **Configured:** the persisted choice, or “Inherit deployment” when unset.
- **Available:** the running deployment permits the implemented capability and
  its required dependencies. This is a hard ceiling.
- **Effective:** the configured/inherited choice, deployment ceiling and parent
  runtime switches all permit the feature. An enabled child can remain ineffective
  while its parent is paused.

Unset preferences preserve the existing deployment behavior. An environment
switch set to false, a missing write account, a restricted Facebook content type,
missing delivery configuration, or an unactivated direct policy cannot be
circumvented by a stored true value. Existing database `meta` preferences remain
separate from `features`; saving one cannot overwrite the other.

| Feature | Deployment ceiling | Runtime behavior |
| --- | --- | --- |
| Facebook Publishing | `META_LIVE_WRITES`, `PUBLISHING_ENABLED`, Facebook in `META_WRITE_ACCOUNT_IDS` | Pause Facebook publication, edit and reply execution. |
| Facebook Images | Facebook ceiling, `FACEBOOK_MEDIA_PUBLISHING_ENABLED`, `image` allowed, signed delivery and public media origin | Pause image publication. |
| Facebook Videos | Same, with `video` allowed | Pause regular video publication. |
| Facebook Reels | Facebook ceiling, media switch and `reel` allowed | Pause native Reel mutations; hosted delivery is not required. |
| Instagram Publishing | Global switches, Instagram allowlist entry, `INSTAGRAM_REELS_PUBLISHING_ENABLED` | Reels only. Other Instagram formats remain unavailable. |
| Media Import | `MEDIA_INGESTION_ENABLED` | Pause ChatGPT imports and authenticated HTTP uploads where the proxy permits that route. |
| ChatGPT Direct Publishing | Deployed `chatgpt_direct` mode, OAuth owner/client restrictions, recorded tests and operator risk acceptance | Off requires independent owner authorization; it never creates a new direct policy. |
| Signed Media Delivery | `MEDIA_DELIVERY_ENABLED` | Pause private/signed GET and HEAD downloads. Facebook images/videos additionally require a configured public delivery origin. |
| Attachment Validation Probe | `MEDIA_ATTACHMENT_PROBE_ENABLED` and exact allowed hosts | Off leaves the hostname-only reference probe available, without downloading media. |
| Scheduling | Unavailable | Live scheduling is not implemented. Mock jobs do not publish. |

A toggle is not approval of content. Authentication, roles/scopes, account
allowlists, immutable previews/hashes, idempotency, content validation and replay
protection are unchanged. Enabling a publishing-related preference first shows
a confirmation page; cancelling makes no change. The warning describes direct
chat reliance and does not claim an independently verifiable consent receipt.

## Persistence, concurrency and failures

Preferences are encrypted using the existing configuration architecture and stored
in the existing `site_configuration` table under `features`. No schema migration
or additional service is required. Each update atomically compares/increments a
revision and inserts an audit record with actor, UTC timestamp, feature, previous
configured/effective values and new configured/effective values. A stale form,
including a stale enable confirmation, receives HTTP 409 and must be reloaded.
First-insert races and transaction deadlocks are refused safely rather than
silently retried. Audit insertion failure rolls back the preference as well.

All mutations require the existing short-lived secure administrator session,
exact same origin, bounded form input and session-bound CSRF token. Extra,
duplicate and unsupported fields are rejected. No feature-management MCP write
tool or unauthenticated settings endpoint is added. Invalid/unreadable stored
configuration fails closed; the application never falls back to enabling writes.

Operations read fresh preferences without an application cache or container
restart. Execution checks the current gates before claiming work and again
immediately before dispatch after asynchronous validation. New requests after
a disable is committed are blocked. A request already sent to Meta cannot be
recalled; do not interpret disabling as deletion or cancellation of delivery.
A pause before dispatch preserves the immutable request/checkpoint for later
explicit resumption. No automatic retry or publication worker is introduced.
GET-only status reconciliation remains available with publishing disabled.
Direct-policy grants are checked against the current policy on every execution.

Disabling imports does not delete assets or stop their existing retention cleanup.
Disabling signed delivery can interrupt an in-progress provider fetch, so check
pending image/video publications before doing so. Switches are changes to
runtime permissions, not commands to publish, replace or retry any operation.

## Restart and deployment requirements

No switch listed as implemented requires a restart when changed through Settings.
Tool registration is stable: an already connected ChatGPT client sees a disabled
feature's safe error rather than needing a new plugin connection.

### Convention for future features

Every new optional implemented capability should have an owner-controlled toggle
in this same section. Following an approved deployment it should be **available
but explicitly off**, ready for the administrator to enable without Codex or a
restart. Seed the off preference before raising its deployment ceiling; exposing
the control should be part of that rollout, not a separate follow-up task.
Use the existing persistence, revision, audit and authorization mechanisms.
Document any technical exception that requires a restart. This convention does
not authorize deployments, social posts, extra accounts or changes to fundamental
security controls. Unsupported functionality remains unavailable.

The following remain operator-managed and require the existing controlled
configuration/deployment procedure, usually a SocialHub-only restart:

- Environment ceilings, global write enablement and permitted Facebook formats.
- Account/identity/client allowlists, roles and OAuth restrictions.
- Base approval policy, activation evidence and accepted residual-risk settings.
- Media hosts, public origins, storage limits, resource budgets and infrastructure.
- The separate `REELS_AI_GENERATED_DEFAULT` content preference. AI disclosure
  remains immutable per prepared Reel; it is not changed by feature switches.

Development does not change production values. In particular, an existing
production media ceiling of false keeps image/video/Reel controls unavailable.
Instagram must stay unavailable until its separate explicit activation is approved.

If the owner later authorizes wider UI control, first persist explicit **off**
preferences for every currently disabled capability, then separately raise only
the approved environment ceilings. Verify the preferences before restarting.
Never raise ceilings with unset/inherited preferences and assume publishing will
stay off. Account restrictions and the approved security policy remain unchanged.

Before deployment, back up the existing private Compose/env and encrypted
configuration/audit rows using the existing procedure. Deploy the reviewed Git
commit and ARM64 image with the existing SocialHub-only restart; do not activate
features or run migrations. Verify unchanged effective states, owner login,
unavailable Instagram, anonymous MCP challenge and read-only account connectivity.
The first UI preference write needs the existing CRUD rights to
`site_configuration` and `audit_logs`; no DDL or new database credentials are needed.

**Rollback:** older application images do not know these restrictive database
preferences and can ignore a UI pause. Before rolling back, explicitly enforce
all current restrictions in the deployment environment/Compose, stop new
mutations and reconcile pending outcomes. Then restore the backed-up image pins
and restart only SocialHub using the documented deployment procedure. Keep
preferences, audit history and immutable publications; never delete/recreate a
publication to recover. Backups and exact production paths belong in ignored
`private/`, not public Git documentation.

## Development validation

On 28 October 2026 the final source passed **522 automated tests** in WSL
(22.76 seconds) and the standard isolated Docker test stage (49.22 seconds).
Both suites skipped two opt-in MariaDB tests and reported three existing Alembic
path-separator deprecation warnings. The two opt-in tests passed separately
against a disposable MariaDB 11.4 instance, covering concurrent initialization
and existing-row revision/audit locking. No existing database was modified.

Chromium over loopback HTTPS verified owner-session access, switching off,
persistence after reload, enable warning/cancellation/confirmation, and disabled
Instagram/scheduling controls. It used a synthetic administrator session rather
than repeating a real OIDC login; the existing OIDC/MFA regression tests passed.
A local ARM64 emulation smoke test verified exact application source hashes,
dynamic persistent import switches, safe write defaults and absence of FFmpeg
encoding/browser/test dependencies. No new production dependency was added.

These development checks did not modify production. Subsequent approved rollout
and configuration changes are recorded separately in ignored operational records;
the development results are not evidence of a live social-media publication.
