# Next planned work: Feature Flag Management UI

Status: proposed; no UI implementation or production activation is included in
the Facebook Reel acceptance follow-up. Prioritize this before Instagram live tests.

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
