# Acknowledged delivery and final status

A successful publishing response first stores a durable `delivered` checkpoint
with the returned remote ID. This prevents a later metadata lookup failure from
losing evidence that Meta already accepted the operation.

Previously, the publication remained `processing` until a second execution call
performed the permalink lookup. The status tool only returned the cached state.
This could show `in_progress` even when the post was publicly visible.

Execution now persists the acknowledgement and then performs read-only delivery
finalization. `get_publication_status` can also refresh these already-delivered
checkpoints. It uses a connector with mutations disabled and Meta GET requests
only, then updates the local result/cache/audit records. It never claims a new
write, grants approval, starts or resumes an upload, creates a replacement
publication, or resends the original operation. Normal status reads still require
the authenticated publication owner. The immutable request hash is checked before
reading and again before committing finalization.

On success, the target and publication become `succeeded`, with the original
remote ID and a permalink when Meta supplies one. The operation's write-attempt
counter does not increase. `delivery_confirmed` distinguishes an acknowledgement
from an operation whose write outcome is unknown. Concurrent refreshes cannot
replace a changed checkpoint, overwrite an active worker or duplicate the local
post cache/success audit event.

If metadata is unavailable, the durable delivery checkpoint remains intact and
the status exposes the sanitized error and a retry time. Status polling waits
at least 60 seconds before trying metadata again; it does not turn that situation
into another publication attempt. Finalization works even when global publishing
is subsequently disabled or an owner grant expires, because it cannot mutate
remote content or mint another grant.

Prepared, failed, uncertain and actively sending targets are not automatically
advanced. Video-processing and Instagram-container stages retain their existing
explicit workflow; this finalizer only handles acknowledged deliveries. Unknown
write outcomes still require the independent verified reconciliation process.
Never create a new idempotency key to work around a pending or uncertain result.
