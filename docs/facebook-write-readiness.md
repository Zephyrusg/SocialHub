# Facebook-only publishing readiness

Reads and writes have separate account policies. `META_WRITE_ACCOUNT_IDS` defaults to
`[]`, which denies all real-account publication requests. Set it explicitly to
`["pixelpapa-facebook"]` to prepare Facebook operations while Instagram stays
read-only. Unknown account names fail configuration validation. Connecting an
account or obtaining Meta permissions does not add it to this list.

The allowlist is enforced during preparation, independent owner authorization,
whole-request execution preflight, durable target claims and execution connector
creation. Existing multi-account requests are rejected in their entirety if any
target is excluded; they cannot partially publish to Facebook first. Changes also
invalidate native-policy fingerprints. Read capabilities remain available for
excluded accounts. `write_account_allowed` describes account policy;
`live_writes_enabled` also requires both global switches.

## Current preparation stage

Keep all of these settings while testing the approval interface:

```dotenv
META_WRITE_ACCOUNT_IDS=["pixelpapa-facebook"]
META_LIVE_WRITES=false
PUBLISHING_ENABLED=false
APPROVAL_MODE=owner_required
CHATGPT_CONFIRMATION_TESTED=false
CHATGPT_RESIDUAL_RISK_ACCEPTED=false
```

This does **not** activate publishing. The optional setup page cannot change these
security settings. Instagram publication, edit and reply requests are denied.

## Approval-interface test without a real publication

1. Verify production reports `live_read_only` and `live_writes_enabled: false`.
2. Ask the host to call the consequential `execute_publication` tool with the
   deliberately nonexistent ID `facebook-approval-interface-probe`. Do not prepare
   a publication with this ID. The tool remains marked `readOnlyHint: false`,
   `destructiveHint: true`, `openWorldHint: true` and `idempotentHint: true`.
3. Record whether the host shows a native confirmation, the exact tool/arguments
   reviewed, the host permission setting and whether rejecting it prevents a
   server request. An accepted request must return `live_writes_disabled`; it
   cannot claim a publication or call Meta. A host-side scope/consent rejection
   is a separate result, not successful confirmation testing.
4. Repeat in the actual ChatGPT integration. A Codex tool invocation or a synthetic
   MCP annotation test does not prove ChatGPT's interface behavior. Store evidence
   privately; do not include credentials, authorization links or identity records.
5. Do not mark confirmation tested or change approval policy automatically.

The current production OAuth client is read-only. Preparing a real preview also
requires `socialhub:publish` in the provider's custom scopes, the exact owner's
client scope allowlist and the newly consented access token. Adding that scope
alone must never activate either global write switch. Review changes to the shared
identity provider separately and preserve unrelated clients and existing MFA.

## Before any actual test post

Review the exact Facebook text, target and immutable hash in ChatGPT. With
`owner_required`, even a native host confirmation still requires an independently
recorded owner grant through the existing CLI or optional portal. A model-supplied
approval boolean is never accepted. A different chat-first policy requires explicit
operator acceptance of the fact that SocialHub cannot independently verify chat
consent, recorded actual host tests, exact OAuth client/subject restrictions and
its existing configuration gates. Do not silently replace `owner_required`.

Enable the two global switches only after these checks and explicit authorization.
Keep the account allowlist Facebook-only. Verify Instagram reports writes disabled,
reject mixed-account requests, preserve idempotency and stop on uncertain outcomes.
Do not perform automatic test posts, edits, comments or deletions.

## Rollback

Turn both global write switches off first. Restore `META_WRITE_ACCOUNT_IDS=[]` to
deny preparation and execution for every account. Recreate only the application
with the reviewed Compose deployment. Do not remove the new setting while rolling
back to older application code: older versions do not enforce the account policy.
Reverting to an older image is safe only while both global write switches remain
off. Existing requests and owner grants remain in the database for audit; an
excluded account cannot use them. Recheck effective runtime gates and both
accounts' read capabilities after rollback.
