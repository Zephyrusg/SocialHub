# Reading Facebook post content

Facebook `list_posts` and `get_post` use the same explicit Graph field selection and content representation. Existing IDs, timestamps and permalinks remain unchanged. The tools also return:

| Field | Meaning when supplied by Meta |
| --- | --- |
| `message` | Actual post text, preserved without rewriting. `null` when unavailable. |
| `story` | Meta-generated activity text, kept separate from the author's message. |
| `full_picture` | Main picture URL, or `null` when unavailable. |
| `attachments.data` | Attachment type, title, description, URL, media metadata and target. |
| `attachments.data[].subattachments.data` | Child photo/video metadata for an album. |

Media metadata can include image source URLs and dimensions. Missing attachments produce an empty `data` list. A cover-photo update can have a story and image without any `message`; the tools do not invent text or substitute a generated story as the author's message.

Attachment and subattachment pagination links are removed because upstream URLs can contain credentials. A supplied continuation is represented by `has_more: true`; nested attachment continuation fetching is not currently exposed. The existing safe outer post cursor remains unchanged.

`get_post` retains the account-owned post lookup. It does not gain an unrestricted arbitrary Graph object lookup, and both operations remain read-only. Permission errors retain the existing actionable Graph error behavior; requesting richer fields does not grant new permissions or activate publishing.

The content regression tests cover text, photo captions, video/album metadata, cover updates without messages, empty/unavailable fields, malformed attachment connections, pagination, ownership and token-bearing link removal. An HTTP MCP test verifies both structured and text tool responses preserve the supplied message and media data.
