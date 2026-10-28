# AI disclosure for Reels

New Facebook and Instagram Reel previews include a resolved boolean
`preview.request.is_ai_generated`. The default is **true**, suitable for the
project's Google Flow workflow. For genuine non-AI footage, explicitly prepare
with `is_ai_generated=false`. Show the resolved value when reviewing the exact
publication in ChatGPT. A disclosure flag does not authorize publication.

Operators can set `REELS_AI_GENERATED_DEFAULT=false` for deployments that primarily
publish camera footage. Omitted/null input resolves before hashing and storage.
Execution never consults the current default. The existing setup page does not
yet expose this preference; the planned Settings controls should show it
separately from publishing gates.

The same immutable value applies to every selected Reel target:

| Platform | Mutation carrying `is_ai_generated` |
| --- | --- |
| Facebook | `POST /{page}/video_reels`, `upload_phase=finish` |
| Instagram | `POST /{ig}/media`, `media_type=REELS`, `upload_type=resumable` |

Both true and false are sent explicitly for new previews. Meta's official SDK
declares the boolean for the [Page video_reels edge](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/page.py)
and [Instagram media edge](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/iguser.py).
Actual live acceptance and display of the label still require an authorized
platform test. Meta controls label rendering and may detect AI independently.
See [Meta's disclosure policy](https://about.fb.com/news/2024/02/labeling-ai-generated-images-on-facebook-instagram-and-threads/).
No caption suffix, media rewriting, transcoding, dependency or public media
endpoint is introduced.

The field currently applies only to `content_type="reel"`. Regular text, images,
videos, edits and replies retain their existing behavior. Non-Reel requests with
an explicit disclosure value are rejected. Strings and integers are rejected;
this is a boolean, not approval evidence.

## Immutable requests and compatibility

Disclosure participates in the existing content hash and approval binding.
Changing it requires a new preview and idempotency key. Reusing a key with a
different resolved value yields `idempotency_conflict`. Changing the default
cannot change a stored preview. When retrying preparation after a default
change, send the previously resolved value explicitly.

Existing prepared/in-progress publications without this field remain untouched
and omit the parameter during execution. Never silently relabel, rehash,
re-upload or duplicate them. Read-only reconciliation stays unchanged. Existing
audit records bind operations to their content hash; no extra identity or token
logging is needed.

## Deployment and rollback

Development and testing happen in WSL. Production deployment and any live
publication require separate owner approval. Preserve account restrictions,
approval policy and media gates; keep Instagram disabled.

1. Follow [the deployment procedure](reels-deployment.md): review the commit,
   build/test ARM64, record the digest and back up current image pins and private
   settings. No database migration, OAuth scope or Nginx change is required.
2. Update production through Git and restart only SocialHub after approval.
   Refresh ChatGPT tools for the optional parameter. Keep gates unchanged and
   inspect a new preview before any live test.
3. For rollback, disable media mutations first. Retain this version to reconcile
   disclosure-bearing in-flight requests. Older code does not forward the flag
   and must not execute those previews. Do not delete history or create replacements.
4. Once operations are terminal and media writes remain disabled, restore the
   prior image/settings from backup and verify health/read-only connectivity.
   Retain the audit trail and immutable requests.

Automated tests check defaults/overrides, multi-account previews, hash and
idempotency binding, strict booleans, MCP schema, platform payload placement and
legacy omission. No real Meta writes are part of development testing.

Validation on 28 October 2026: **493 tests passed in WSL** (20.16 seconds) and
**493 passed in the standard Docker test stage** (45.41 seconds), using mocked
Meta responses and pre-generated media. Both runs reported three existing
Alembic path-separator deprecation warnings. Production and live labels were
not tested or changed by this implementation.
