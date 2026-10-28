# Media POST timeouts and owner reconciliation

A live Facebook image test published successfully at Meta, but SocialHub reached
its ten-second Graph HTTP timeout before receiving an acknowledgement. It stored
an uncertain outcome, with one attempt and no remote ID. This was deliberately
not retried: lack of an acknowledgement does not prove that publishing failed.
The exact network timeout phase was not recorded by that older runtime.

The owner independently supplied the existing post ID. Read-only Page checks
verified the exact caption, a single photo and creation within the execution
window; the original asset and immutable request hashes were also checked. The
existing publication was reconciled to succeeded and cached once, without any
Meta mutation. Its attempt count stayed one. Media publishing was disabled after
the test; video and Instagram publishing were never enabled.

## Prevention

Photo/video Graph POSTs now allow 120 seconds of response-read inactivity, ten
seconds to connect/acquire a connection and 30 seconds for sending the small
request. Ordinary reads, text operations and OAuth retain their ten-second
limits. These HTTP timeouts fit within the existing five-minute sending lease;
they are phase/inactivity limits, not a guarantee against every slow response.
The mutation still has exactly one attempt. A timeout remains uncertain and
requires reconciliation; the server does not guess an outcome from a feed search
or manufacture a replacement publication.

Timeout audits retain only an allowlisted phase (`connect`, `read`, `write`,
`pool` or `unknown`), never an exception string, capability URL or credentials.
Longer media acknowledgement waits and continued no-retry behavior are covered
by synthetic HTTP tests. No extra public post was made to test this change.

## Administrative reconciliation

The trusted local-owner reconciliation path stays outside MCP and public HTTP.
The CLI displays the stored preview and asks the owner to confirm a known remote
ID; it passes the reviewed request hash to the service. The owner must first
inspect that exact result. Normal ChatGPT publishing needs no extra portal or
terminal approval; this administrative fallback handles ambiguous outcomes.

Reconciliation checks Page ownership through the connector, matching text,
freshness relative to recorded execution rather than preparation, an authorized
used grant, at least one original attempt and absence of an active lease. For a
Facebook image it also requires exactly one photo attachment. It rejects a
remote post already bound to another publication. State, attempt count and
immutable hash are rechecked under the database lock after the external read.
It records a delivered checkpoint, clears the old error/retry time, caches the
post once and audits the owner-selected binding without another write attempt.

Meta can resize/re-encode photos: remote bytes are not asserted to match the
original SHA-256. The trusted owner's explicit remote-ID selection is essential;
caption, timestamp and attachment type alone are not proof of identical media.
Video/Reel and reply reconciliation remain manual cases until dedicated outcome
readers can verify their evidence. Missing, deleted, ambiguous or inaccessible
posts must never be automatically resubmitted.

Private operational notes contain the exact test IDs, timing, read-only proof,
backups, deployed image/source hashes and rollback commands. Completed status
reads remain succeeded without contacting Meta, including after owner deletion.
