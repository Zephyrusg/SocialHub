# SocialHub feature development

- Give every new optional implemented capability an administrator-controlled
  toggle in the existing Settings page, using the shared feature registry,
  persistent configuration, revisions and audit logging.
- New optional capabilities should arrive available but explicitly off after
  an approved rollout. Initialize the off preference before raising a deployment
  ceiling; do not require a second Codex intervention merely to expose the toggle.
- Changes to runtime preferences should take effect without restarting SocialHub.
  Document any unavoidable restart requirement before rollout.
- Preserve server-side administrator authorization, CSRF/origin checks,
  concurrency handling and deployment security ceilings.
- Feature toggles never grant account access, replace publication approval,
  weaken validation/idempotency or initiate autonomous publishing.
- Do not present unimplemented functionality as usable. Keep unsupported controls
  unavailable and explain why.
- Test that each toggle affects its backend operation and that disabling it stops
  new execution. Production deployments and real publications still need their
  applicable explicit authorization.
