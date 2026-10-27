# Chat-first approval policies

The default is `APPROVAL_MODE=owner_required`. Independent owner grants remain
available through the CLI or optional portal. Enabling an account, granting an
OAuth scope, or setting a model-supplied approval flag never changes this policy.

Two explicit operator-selected alternatives share the same publication state
machine, immutable preview hashes, verified OAuth identities, publisher roles,
account restrictions, idempotency, audit trail and uncertain-outcome handling:

| Policy | Recorded host test | Trust decision |
| --- | --- | --- |
| `chatgpt_native` | `CHATGPT_CONFIRMATION_TESTED` and its evidence hash | Rely on the host's native confirmation behavior |
| `chatgpt_direct` | `CHATGPT_DIRECT_TESTED` and its separate evidence hash | Rely on the authenticated client acting on explicit user instructions |

Both require `CHATGPT_RESIDUAL_RISK_ACCEPTED=true`, exact client/subject allowlists,
allowed operations and OAuth scopes. Neither policy produces an independently
verified consent receipt. The server sees an authenticated invocation, not the
user's chat conversation. A mistaken or compromised client, prompt injection or
an unintended invocation could therefore initiate a write within those limits.
A model statement that the user consented is never accepted as server evidence.

`chatgpt_direct` does not require a second confirmation popup or claim one was
tested. The host must still respect the user's instructions and any confirmations
it presents. Keep its native-confirmation test flag false when unverified. Tests
and consent evidence for the two modes are separate and cannot substitute for
each other. Direct-mode host testing must include preparation and a blocked
execution while publishing is disabled; record the actual observed behavior and
limitations privately before accepting the risk and activating the policy.

## Explicit configuration

An operator can choose direct-mode Facebook publication after those checks:

```dotenv
APPROVAL_MODE=chatgpt_direct
CHATGPT_DIRECT_TESTED=true
CHATGPT_DIRECT_EVIDENCE_SHA256=SHA256_OF_PRIVATE_TEST_AND_OPERATOR_ACCEPTANCE_RECORD
CHATGPT_CONFIRMATION_TESTED=false
CHATGPT_RESIDUAL_RISK_ACCEPTED=true
CHATGPT_ALLOWED_CLIENT_IDS=["EXACT_REGISTERED_CLIENT"]
CHATGPT_ALLOWED_PRINCIPALS=["EXACT_VERIFIED_OWNER_SUBJECT"]
CHATGPT_ALLOWED_OPERATIONS=["publish"]
META_WRITE_ACCOUNT_IDS=["pixelpapa-facebook"]
```

These policy settings alone do not turn publishing on. Actual execution also
requires `PUBLISHING_ENABLED=true`, `META_LIVE_WRITES=true`, an unexpired verified
access token with read/publish scopes, the publisher role, a matching immutable
request and current account capabilities. Other clients, subjects, operations,
accounts and mixed-account requests are refused by client-policy authorization;
an independently recorded owner grant remains a separate stronger channel.
Preparing a preview does not
itself authorize execution. Each execution must follow an explicit user request
for that exact reviewed operation; no automatic posting or scheduling is enabled.

Evidence must honestly document what was tested and the operator's policy choice.
Do not set a tested flag based on a model assertion or invent a native confirmation
result. Keep operational evidence and identity data in ignored private storage.
The public examples are placeholders and keep default configuration owner-required
and both global write switches off.

## Revocation and rollback

Disable both global write switches to halt new dispatches. Switching policy back
to `owner_required` refuses existing client-policy grants; it does not turn them
into owner grants. A completed remote post is never automatically deleted on
rollback. Policy fingerprints include mode, test evidence and identity/account/
operation allowlists; changing them stops reuse of earlier policy grants. Expired
or revoked grants never renew silently. Do not automatically retry unknown remote
outcomes or substitute a new publication to bypass a revoked grant.
