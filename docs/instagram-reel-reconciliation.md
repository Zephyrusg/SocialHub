# Recovering an uncertain Instagram Reel publication

An Instagram container reporting `FINISHED` is ready for the final
`media_publish` call. A missing response to that call must not be treated as proof
of failure: the Reel may already be published. Meta's
[documented publishing flow](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api?entity=request-23987686-3fe78620-2258-44b6-893f-42d76c7200d7)
separates container status checks from final publication.

SocialHub uses a 120-second read allowance for `media_publish`, matching its
existing photo/video finalization allowance. The connect/pool bounds stay ten
seconds and the media-write bound thirty seconds. Every mutation still has
**one attempt**. Longer waiting reduces premature uncertainty; it does not make
retrying an unacknowledged write safe.

## Trusted local recovery

The existing administrative `scripts/reconcile_publication.py` supports an
uncertain Instagram Reel when there is verified processing evidence. It remains
a trusted local owner/administrator action, not an MCP tool or a model-supplied
approval flag. The normal publishing approval policy is unchanged. No additional
runtime feature switch controls this recovery safety mechanism.

First inspect the original publication, saved container and account-owned media
using GET requests. Never resubmit `media_publish`, create another container,
replace the publication/media object or alter the immutable preview to repair a
status display.

Recovery requires:

- The original unchanged preview/hash, a recorded used authorization and an
  uncertain target with no active lease.
- The persisted `ig_reel_ready` checkpoint and an audit record proving the
  attempted finish for that exact target/container.
- A connector with writes disabled and a fresh GET confirming that saved
  container reports `PUBLISHED`.
- An owner-selected published media ID appearing in the authorized account's own
  paginated listing as `VIDEO` / `REELS`, with the exact approved caption.
- Exactly one matching candidate in the service's recovery window: from two
  minutes before authorized execution through fifteen minutes after the recorded
  uncertain finish. These are conservative service bounds, not Meta guarantees.
- A valid canonical Instagram Reel permalink and a complete bounded listing.
  Repeated cursors, more than ten pages, ambiguous candidates, missing dates,
  unrelated IDs or media already bound to another publication are rejected.

The container status does not directly provide the published media ID. This
procedure records explicit trusted correlation of the published container,
account ownership, exact caption and timing. It does not claim that Meta's
transcoded remote bytes have the same hash as the imported file.

After those GETs succeed, an atomic local update rechecks the preview, target,
checkpoint, attempt count and lease. It marks only that target delivered, stores
the published media ID/permalink, retains the original container ID, clears the
stale error/retry state and appends a reconciliation audit event. Existing
publication/media IDs, request hash, authorization, prior audit records and
mutation attempt count remain unchanged. Other platforms are not dispatched.

Read-only recovery also works with live-writing switches disabled. Already
delivered publications retain their normal replay protection. Status polling
does not automatically guess a remote ID or send a missing mutation.

## Operational use

From a trusted local administrator terminal in the normal application environment:

```sh
python scripts/reconcile_publication.py PUBLICATION_ID TARGET_ID PUBLISHED_MEDIA_ID
```

Review the exact preview and enter only the independently verified published
media ID when prompted. Do not use the container ID as the published media ID.
If evidence is incomplete or ambiguous, keep the state uncertain and investigate
with more GETs. No replacement publication is justified by a timeout alone.

Application code must be installed through the normal tested Git/image release
procedure before using the extended recovery path. Operational evidence, actual
account/publication IDs and exact deployment/rollback commands stay in ignored
private records. No schema, role, permission or feature-preference migration is
needed for this fix.
