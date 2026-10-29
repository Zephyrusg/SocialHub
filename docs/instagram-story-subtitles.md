# Phase 3H.2.1 — configurable Story subtitle

Development candidate pending visual review; not deployed. The two real-cover
previews are produced through isolated local storage/render/transfer flows.
Production and publication approval boundaries remain unchanged.

`prepare_instagram_artwork` keeps its existing name and gains optional Story inputs:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `subtitle_mode` | `hidden` | `hidden` omits the subtitle; `title` displays the supplied title under OUT NOW. |
| `song_title` | omitted | Required and non-blank for `title`; up to 200 characters. |
| `announcement` | empty | Still accepted and returned for compatibility, but never rendered. |

The existing main OUT NOW heading, original sharp cover, blurred atmospheric
background, branding, Music sticker space and bottom UI safe area remain. Default
hidden rendering is byte-for-byte identical to the previously approved clean
cinematic design. No OCR or external service is used. In title mode the title is
centered, measured in pixels, wrapped to at most three lines and reduced from
40 px to 36/32 px if needed. The cover moves only enough to preserve spacing and
always ends above the artist branding. Titles that still cannot fit safely are
rejected with a shortening instruction; no characters are silently truncated.
Unsupported glyphs receive the existing explicit text error.

Examples, with the same imported cover but distinct idempotency keys:

```json
{"media_id":"OWNED_COVER_ID","idempotency_key":"story-hidden-new","purpose":"story","subtitle_mode":"hidden"}
```

```json
{"media_id":"OWNED_COVER_ID","idempotency_key":"story-title-new","purpose":"story","subtitle_mode":"title","song_title":"It's Dangerous to Go Alone"}
```

Returned `effective_artwork_inputs` records normalized subtitle mode/title and the
legacy announcement. `announcement_rendered=false` makes its compatibility-only
status explicit. These visual inputs do not select or verify any Instagram Music
recording. The existing artist/song guidance and native Music selection remain a
separate assisted workflow. No Story or feed post is published by preparation.

The updated render profile is `cinematic-release-v2.1`. Every new Story request
hash binds its source ID/checksum, purpose, announcement, render profile, subtitle
mode and trimmed song title. Changes conflict under an existing key even when the
title is hidden or an ignored announcement happens to produce identical bytes.
Fresh requests must use fresh keys. In particular, pre-subtitle v1/v2 Story keys
are refused rather than returning an older image with a duplicate announcement.
Existing stored images/records are retained and previously issued signed links
remain valid for their normal lifetime. No stored asset is silently replaced.
Feed request hashes remain exactly v1; old feed keys, image normalization and
publication workflows remain unchanged. Non-default Story subtitle options are
refused for feed preparation.

This is a text-layout preference inside the existing Story artwork operation,
controlled by the existing administrator Story Artwork switch and its dependencies;
no separate execution capability, feature ceiling or style switch is added.
Disabling Story Artwork stops new preparation, including cached-key requests.
Source ownership, media quotas, retention, signed transfer/download validation,
permissions, runtime flag refresh, CSRF/origin protection on admin actions and
publication approval safeguards remain in use. No migration is needed.

Any eventual production rollout still requires visual approval and explicit
rollout authorization. Merge the accepted branch into main and push main before
production deployment; preserve the owner's current feature preferences. Clients
may need metadata refresh plus a new chat to discover the optional MCP arguments.

Validation: **677 tests passed; 2 optional disposable-MariaDB checks skipped**.
New coverage includes required-title validation, ignored announcements, long-title
wrapping and safe text bounds, mode/title idempotency conflicts, refusal of older
Story render keys, unchanged legacy feed hashes, runtime disable behavior and
MCP optional-schema/argument forwarding. Both real-cover variants passed local
signed phone-page and download checks. The hidden preview matches the previously
approved JPEG checksum exactly. No production changes or publications occurred.
