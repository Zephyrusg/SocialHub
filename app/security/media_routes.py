import asyncio
import os
import tempfile
from pathlib import Path
from starlette.responses import JSONResponse, FileResponse
from app.core import HubError
from app.security.auth import Verifier
from app.services.media_manager import MediaManager


def register_media_routes(mcp, settings, sessions):
    manager = MediaManager(settings, sessions)
    verifier = Verifier(settings)
    upload_slots = asyncio.Semaphore(1)

    @mcp.custom_route('/media/upload', methods=['POST'])
    async def upload(request):
        if not settings.media_delivery_enabled:
            return JSONResponse({'error': 'media_disabled'}, status_code=404)
        token = await verifier.verify_token(request.headers.get('authorization', '').removeprefix('Bearer '))
        if token is None or 'socialhub:media' not in token.scopes:
            return JSONResponse({'error': 'media_scope_required'}, status_code=403)
        root = Path(settings.media_root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.upload-', dir=root)
        total = 0
        try:
            with os.fdopen(fd, 'wb') as output:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > settings.media_max_bytes:
                        raise HubError('invalid_media', 'Upload exceeds configured size limit')
                    output.write(chunk)
            async with upload_slots:
                result = await asyncio.to_thread(manager.save, name, token.client_id)
            return JSONResponse({'ok': True, 'media': result}, headers={'Cache-Control': 'no-store'})
        except HubError as error:
            return JSONResponse({'ok': False, 'error': {'code': error.code, 'message': error.message}}, status_code=400)
        finally:
            Path(name).unlink(missing_ok=True)

    @mcp.custom_route('/media/{media_id}', methods=['GET', 'HEAD'])
    async def download(request):
        if not settings.media_delivery_enabled:
            return JSONResponse({'error': 'media_disabled'}, status_code=404)
        mid = request.path_params['media_id']
        try:
            token = await verifier.verify_token(request.headers.get('authorization', '').removeprefix('Bearer '))
            if token and 'socialhub:media' in token.scopes:
                manager.get(mid, token.client_id)
                path, mime = await asyncio.to_thread(manager.path, mid)
            else:
                if len(request.query_params.getlist('expires')) != 1 or len(request.query_params.getlist('signature')) != 1:
                    raise HubError('media_access_denied', 'Media authorization required')
                path, mime = await asyncio.to_thread(manager.verify_signature, mid, request.query_params.get('expires'), request.query_params.get('signature'))
            return FileResponse(path, media_type=mime, headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'})
        except HubError:
            return JSONResponse({'error': 'media_access_denied'}, status_code=403, headers={'Cache-Control': 'no-store'})
