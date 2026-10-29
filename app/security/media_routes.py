import asyncio
import os
from pathlib import Path
from starlette.responses import JSONResponse, FileResponse
from app.core import HubError
from app.security.auth import Verifier
from app.services.media_manager import MediaManager
from app.services.media_budget import MediaBudget
from app.services.media_ingestion import validation_thread
from app.services.administration import require_publisher


def register_media_routes(mcp, settings, sessions):
    manager = MediaManager(settings, sessions)
    verifier = Verifier(settings)
    budget = MediaBudget(settings, sessions)

    @mcp.custom_route('/media/upload', methods=['POST'])
    async def upload(request):
        try:enabled = settings.media_ingestion_enabled
        except Exception:return JSONResponse({'error': 'configuration_unavailable'}, status_code=503, headers={'Cache-Control':'no-store'})
        if not enabled:
            return JSONResponse({'error': 'media_disabled'}, status_code=404)
        if len(request.headers.getlist('authorization')) != 1:
            return JSONResponse({'error': 'media_scope_required'}, status_code=403)
        token = await verifier.verify_token(request.headers.get('authorization', '').removeprefix('Bearer '))
        if token is None or not ({'socialhub:media', 'socialhub:publish'} & set(token.scopes)):
            return JSONResponse({'error': 'media_scope_required'}, status_code=403)
        try:
            require_publisher(settings, token.client_id)
            length = request.headers.get('content-length')
            if length is not None and (len(length) > 20 or not length.isdecimal() or not 0 < int(length) <= settings.media_max_bytes):
                raise HubError('invalid_media', 'Upload exceeds the limit or has an invalid length')
            with budget.upload() as path:
                total = 0
                with path.open('wb') as output:
                    async with asyncio.timeout(settings.media_receive_timeout_seconds):
                        async for chunk in request.stream():
                            total += len(chunk)
                            if total > settings.media_max_bytes:
                                raise HubError('invalid_media', 'Upload exceeds configured size limit')
                            await asyncio.to_thread(output.write, chunk)
                if length is not None and total != int(length):
                    raise HubError('invalid_media', 'Upload is incomplete')
                result = await validation_thread(manager.save, path, token.client_id, consume=True)
            return JSONResponse({'ok': True, 'media': result}, headers={'Cache-Control': 'no-store'})
        except HubError as error:
            status = 429 if error.code in {'media_busy', 'media_quota_exceeded', 'media_storage_full'} else 400
            return JSONResponse({'ok': False, 'error': {'code': error.code, 'message': error.message}}, status_code=status, headers={'Cache-Control': 'no-store'})
        except TimeoutError:
            return JSONResponse({'error': 'media_receive_timeout'}, status_code=408)
        except OSError:
            return JSONResponse({'error': 'media_storage_unavailable'}, status_code=503)
        except Exception:
            return JSONResponse({'error': 'media_upload_failed'}, status_code=503)

    @mcp.custom_route('/media/{media_id}', methods=['GET', 'HEAD'])
    async def download(request):
        try:enabled = settings.media_delivery_enabled
        except Exception:return JSONResponse({'error': 'configuration_unavailable'}, status_code=503, headers={'Cache-Control':'no-store'})
        if not enabled:
            return JSONResponse({'error': 'media_disabled'}, status_code=404)
        mid = request.path_params['media_id']
        try:
            if len(request.headers.getlist('authorization')) > 1:
                raise HubError('media_access_denied', 'Ambiguous authorization is refused')
            ranges = request.headers.get('range', '')
            if len(ranges) > 128 or ',' in ranges:
                raise HubError('media_access_denied', 'Only a bounded single byte range is supported')
            token = await verifier.verify_token(request.headers.get('authorization', '').removeprefix('Bearer '))
            if request.headers.get('authorization'):
                if not token or not ({'socialhub:media', 'socialhub:publish'} & set(token.scopes)):
                    raise HubError('media_access_denied', 'Private media authorization required')
                manager.get(mid, token.client_id)
                path, mime = await asyncio.to_thread(manager.path, mid)
            else:
                if len(request.query_params.getlist('expires')) != 1 or len(request.query_params.getlist('signature')) != 1:
                    raise HubError('media_access_denied', 'Media authorization required')
                path, mime = await asyncio.to_thread(manager.verify_signature, mid, request.query_params.get('expires'), request.query_params.get('signature'))
            extension = {'image/jpeg': '.jpg', 'image/png': '.png', 'video/mp4': '.mp4'}[mime]
            filename = 'socialhub-' + mid + extension if request.query_params.get('download') == '1' else None
            return FileResponse(path, media_type=mime, filename=filename, headers={
                'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'})
        except HubError:
            return JSONResponse({'error': 'media_access_denied'}, status_code=403, headers={'Cache-Control': 'no-store'})
