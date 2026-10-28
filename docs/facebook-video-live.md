# Controlled Facebook video acceptance

An owner-selected Google Flow MP4 (H.264/AAC, 720 × 1280, 10.005 seconds) passed
actual ChatGPT transfer, bounded validation, import and immutable preview checks.
The owner separately authorized temporary Facebook video-only publishing and
instructed the connected production ChatGPT plugin to execute that exact request.
Image and Instagram publishing remained disabled during this test.

One `/videos` POST returned a durable video ID and a processing checkpoint, with
one attempt and no API error. The acknowledgement took approximately twelve
seconds; the previous ten-second HTTP budget would have been too short. Read-only
status reconciliation later observed ready and finalized the publication without
another claim, upload or mutation. Status polling continued with media publishing
already disabled again.

Before cleanup, independent GETs verified published=true, ready status, the exact
caption, Page ownership, duration and membership in the Page's video list. Meta
returned a relative `/reel/.../` permalink. Facebook link normalization now returns
an absolute HTTPS Facebook URL, including from video-processing finalization;
malformed, credential-bearing and non-Facebook links are not surfaced as permalinks.
The complete automated suite passes 409 tests.

The owner explicitly authorized deletion after a successful check. A separate
administrative cleanup durably recorded that instruction, rechecked the exact
immutable request and remote video ID, and sent one DELETE. Meta acknowledged it;
GETs verified absence from the owned list and loss of access to the removed object.
No replacement, publish retry or general MCP deletion permission was enabled.

Publication history stays succeeded/delivery-confirmed with its original hash,
one attempt, canonical pre-deletion URL and a cleanup marker. Completed status
reads do not contact the deleted object or downgrade the confirmed history.

This is one controlled acceptance test, not a guarantee for every video or a
production-wide media activation. The operator separately accepted the unresolved
static ffprobe Brotli risk for the limited test; patch/provenance review remains
deferred and must not be described as complete. Exact IDs, timing, authorization,
read evidence, cleanup audit, backup paths and rollback commands remain in ignored
private operational notes. The test media bytes follow the normal temporary
retention policy. Facebook media and Instagram writes are disabled after testing;
the existing text-publishing policy is preserved.
