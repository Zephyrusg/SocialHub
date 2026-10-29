"""Authenticated preparation and expiring phone transfer; neither action publishes."""
import asyncio
import html
import secrets
from typing import Annotated, Literal, Any
from pydantic import Field
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import ToolAnnotations
from starlette.responses import HTMLResponse, JSONResponse
from app.core import HubError
from app.services.instagram_artwork import InstagramArtwork, STEPS

SHARE_SCRIPT = '''
const button = document.getElementById('share');
const feedback = document.getElementById('feedback');
const link = document.getElementById('download');
let shareFile;
async function loadShareFile() {
  if (!navigator.canShare || !navigator.share) return;
  try {
    const response = await fetch(link.href, {credentials: 'omit', cache: 'no-store'});
    if (!response.ok) throw new Error('expired');
    const file = new File([await response.blob()], 'pixelpapa-music-story.jpg', {type: 'image/jpeg'});
    if (!navigator.canShare({files: [file]})) return;
    shareFile = file;
    button.hidden = false;
  } catch (_) { /* Save artwork remains available. */ }
}
loadShareFile();
button.addEventListener('click', async () => {
  try {
    await navigator.share({files: [shareFile]});
    feedback.textContent = 'Share completed. Select the Music track and publish inside Instagram. SocialHub has not verified publication.';
  } catch (error) {
    feedback.textContent = error.name === 'AbortError' ? 'Sharing cancelled. Nothing was published by SocialHub.' : 'File sharing is unavailable or the link expired. Use Save artwork, or request a fresh transfer link in ChatGPT.';
  }
});
'''


def register_instagram_tools(mcp, settings, sessions):
    artwork = InstagramArtwork(settings, sessions)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read', 'socialhub:publish']}]})
    async def prepare_instagram_artwork(
        media_id: Annotated[str, Field(min_length=36, max_length=36, pattern=r'^[a-f0-9-]{36}$')],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=128)],
        purpose: Literal['feed', 'story'] = 'story',
        announcement: Annotated[str, Field(max_length=140)] = '',
        subtitle_mode: Literal['title', 'hidden'] = 'hidden',
        song_title: Annotated[str, Field(max_length=200)] | None = None,
    ) -> dict[str, Any]:
        """Create a JPEG copy from an owned imported cover. Feed: normalize aspect/size without cropping, then use the returned media ID in prepare_publication with content_type image and pixelpapa-instagram; review and obtain explicit publication approval. Story: prepare a cinematic 1080x1920 poster with OUT NOW and PIXELPAPA branding, plus a private phone transfer page. Subtitle defaults hidden. Use subtitle_mode title with a non-empty song_title to display it under OUT NOW. announcement is accepted for compatibility but is not rendered. No OCR is performed. Choose PixelPapa — It's Dangerous to Go Alone from the native Music sticker and select its excerpt inside Instagram; publish manually there. Track availability is unverified; there is no supported automatic library lookup. Transfer/share never means published. No embedded audio, video rendering or native-app intent bridge. Reusing the same key refreshes expiring links to the same asset; changed inputs need a new key. Requires the relevant Settings toggle."""
        token = get_access_token()
        try:
            if token is None:raise HubError('unauthenticated', 'Authenticated MCP context required')
            if 'socialhub:publish' not in token.scopes:raise HubError('insufficient_scope', 'Artwork preparation requires publish scope')
            return {'ok': True, 'data': await artwork.prepare(token.client_id, media_id, idempotency_key, purpose, announcement, subtitle_mode=subtitle_mode, song_title=song_title)}
        except HubError as error:
            return {'ok': False, 'error': {'code': error.code, 'message': error.message, 'retryable': error.retryable}}
        except Exception:
            return {'ok': False, 'error': {'code': 'artwork_failed', 'message': 'Artwork preparation failed; no publication attempted', 'retryable': False}}

    @mcp.custom_route('/instagram/story/{media_id}', methods=['GET'])
    async def transfer(request):
        headers = {'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'}
        try:
            if not settings.instagram_story_artwork_enabled or not settings.media_delivery_enabled:
                raise HubError('instagram_artwork_disabled', 'Story artwork transfer is disabled')
            query = request.query_params
            if any(len(query.getlist(key)) != 1 for key in ('expires', 'signature')):
                raise HubError('media_access_denied', 'One valid transfer signature is required')
            mid = request.path_params['media_id']
            _, mime = await asyncio.to_thread(artwork.media.verify_signature, mid, query['expires'], query['signature'])
            asset = artwork.media.get(mid)
            if mime != 'image/jpeg' or (asset['metadata']['width'], asset['metadata']['height']) != (1080, 1920):
                raise HubError('invalid_media', 'Choose prepared vertical Story artwork')
            # Construct only local URLs from verified IDs/signatures; no external scripts or tracking.
            from urllib.parse import urlencode
            url = '/media/' + mid + '?' + urlencode({'expires': query['expires'], 'signature': query['signature']})
            nonce = secrets.token_urlsafe(24)
            headers['Content-Security-Policy'] = f"default-src 'none'; img-src 'self'; connect-src 'self'; script-src 'nonce-{nonce}'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            steps = ''.join('<li>' + html.escape(step) + '</li>' for step in STEPS)
            page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>PixelPapa Music Story</title>
<h1>Finish your Music Story in Instagram</h1><p>Artwork prepared. Publication and official music availability are unverified.</p>
<img src="{html.escape(url, quote=True)}" width="270" height="480" alt="Prepared Story artwork">
<p><a id="download" href="{html.escape(url+'&download=1', quote=True)}" download="pixelpapa-music-story.jpg">Save artwork</a></p>
<button id="share" hidden>Share artwork</button><p id="feedback" role="status">Share uses your phone's chooser. Instagram and its Story composer may not be offered. Saving always requires you to open Instagram and select the image.</p>
<p>Artist: <strong>PixelPapa</strong><br>Song: <strong>It's Dangerous to Go Alone</strong></p><ol>{steps}</ol>
<script nonce="{nonce}">{SHARE_SCRIPT}</script></html>'''
            return HTMLResponse(page, headers=headers)
        except HubError as error:
            return JSONResponse({'error': error.code}, status_code=403, headers=headers)
        except Exception:
            return JSONResponse({'error': 'transfer_unavailable'}, status_code=503, headers=headers)
