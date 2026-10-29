# Phase 3H.2 — cinematic Story artwork

Implemented on the development branch, pending the owner's visual review before
any production default is replaced. No deployment or real publication is part of
this review. Merge the accepted branch into main and push main before an eventual
explicitly authorized production rollout.

`prepare_instagram_artwork` retains the same arguments and phone transfer flow.
For `purpose=story`, fresh keys create a cinematic 1080×1920 JPEG from the supplied
cover. `purpose=feed` retains the previous image conversion and rendering logic.

The layout is in `app/services/story_templates.py`. It uses an enlarged, strongly
blurred copy of the same cover as a full-bleed background, a gentle midnight-blue
grade and lower gradient, a subtle cover-colored glow, soft shadow and sharp
uncropped cover fitted in an 840 px square. Actual cover colors determine the
accent; warm tones are preferred when present, and cool/purple covers keep their
own hue. Accent lightness is raised when needed for readable typography.

The large OUT NOW heading sits below the top UI area. PIXELPAPA and NEW SINGLE
sit below the cover. The native Music sticker region at y=1460–1620 is left clear;
no critical content occupies the bottom 300 px. There is no fake sticker or extra
rendering of the recording title. OUT NOW is the requested visual heading, not
a release-date check, schedule or claim that anything has been published.

Leave `announcement` empty for the standard design. Extra announcement text is
measured in pixels and wrapped to at most three lines above the cover; the cover
moves down slightly to keep spacing. Text that cannot fit is rejected rather
than clipped. The bundled, unmodified DejaVu Serif Bold and DejaVu Sans fonts
support bullets, typographic dashes/quotes and accented text. Unsupported glyphs
and invisible controls receive a clear error instead of a missing-glyph box.
Add unsupported emoji or stickers inside Instagram if desired.

The fonts and license are included in the installed wheel, so rendering needs no
system font install, network request, external design service or AI generation.
Blur and lighting masks use a 360×640 working image before final scaling. A local
four-sample render took under half a second with about 57 MiB peak process RSS;
this is a development-machine sample, not a native Pi performance guarantee.

New Story outputs bind the `cinematic-release-v2` profile into their idempotency
hash and record it in the response/audit. Existing `v1` keys still return their
original saved artwork and refresh its private transfer links. An old key does
not silently replace an immutable asset with a new style; use a fresh key to
request the cinematic derivative. Changed source, purpose or text still conflict.
Original imported bytes, ownership checks, bounded storage admission, retention,
hash verification and signed download lifetime remain in use.

This is an improvement to the existing default Story renderer, not an additional
optional operation/style control. The existing Instagram Music Story Artwork
administrator switch continues to gate it, including cached results, and disabling
that switch stops new preparation. No new execution pathway bypasses the shared
feature registry. An eventual reviewed rollout must preserve the owner's existing
feature preferences and account/security ceilings. No schema migration is needed.
Publication approval, feed checkpoints/idempotency, Creator account type and the
manual Instagram Music/publication boundary are unchanged. Download/share never
marks a Story as published.

Visual review artifacts are kept in ignored private storage: the actual single
cover poster, its announcement/punctuation variants and a distinctly teal
synthetic cover used only as a palette/layout stress test. The real release cover
is not committed as a test fixture or uploaded to a third-party design service.

Validation for this development candidate: **668 tests passed, 2 optional MariaDB
checks skipped**. The real cover also passed a separate local end-to-end
prepare/transfer/download check: the signed download matched the derived JPEG,
the source checksum stayed unchanged, and disabling the Story flag blocked its
transfer page. Existing publication and feed regressions passed. The built wheel
contains the template module, all three exact font binaries and their license.
The full-size real-cover, announcement/bullet and teal test outputs were inspected.
No actual mobile share chooser or production rollout was exercised.
