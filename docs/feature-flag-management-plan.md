# Feature Flag Management UI — implementation and future convention

Status: implemented in the existing Settings page. The design below records the
original plan; see [feature flags](feature-flags.md) for actual behavior and the
owner's convention for future optional features. Implemented features should
arrive available but explicitly off after an approved rollout, so the owner can
enable them independently without Codex or a restart.

The owner's AI disclosure request takes priority over this UI. Implement the
[Reel disclosure preference](ai-disclosure.md) first; add its default to the
future Settings page separately from write activation. Changing that default
must never alter an existing immutable publication.

Extend the existing authenticated Settings/setup page using the existing owner's
admin identity and recent MFA. Do not require new accounts. Publishing permission
remains separate from setup/admin permission; broader identity-provider admin
group membership must not grant SocialHub access automatically.

The design should:

- Show effective publishing gates per platform/content type and explain their
  relationship to global switches, account restrictions and publishing permission.
- Support intentional owner changes with CSRF/origin protection, bounded input,
  revision/conflict checks and an audit of old/new values and authenticated actor.
- Define one clear source of truth and its precedence over runtime/Compose values;
  avoid controls that appear enabled while another configuration layer overrides them.
- Keep account allowlists, roles, credential management and immutable publication
  approval separate. A feature toggle is not consent to publish specific content.
- Keep consequential MCP operations marked as writes. Never accept a model-supplied
  flag as user approval, and do not expose unauthenticated configuration controls.
- Preserve idempotency and in-flight checkpoints when disabling writes. Explain
  that already dispatched requests cannot be recalled; GET-only reconciliation
  remains possible while new mutations are blocked.
- Keep Instagram disabled until its separately authorized account/permission,
  resumable upload and live acceptance checks are complete.

Implementation should start in WSL with authorization/configuration tests and a
reviewed deployment/rollback plan. Production changes and any live social-media
test need explicit authorization. No feature activation or additional post is
authorized by this planning document.

## Phase 3D.1 implementation

The initial management section is implemented in WSL. See
[feature flags](feature-flags.md) for the current switches, hard deployment
ceilings, encrypted persistence, revision/audit handling, dynamic enforcement and
rollback requirements. This replaces the proposed design above where implemented.
AI disclosure remains supported per immutable Reel; its deployment default is a
separate content preference and is not a publishing activation switch.
Production deployment and changes to its ceilings remain separately authorized.
