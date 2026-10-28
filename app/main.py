import logging
import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any, Literal
from pydantic import Field, AnyHttpUrl, StrictBool
from urllib.parse import urlsplit
from app.security.origins import OriginBoundary
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from app.config import Settings
from app.core import HubError, configure_logging
from app.database import database
from app.security.auth import Verifier
from app.security.tokens import TokenVault
from app.services.hub import Hub
from app.services.administration import EffectiveSettings
from app.services.publishing import PublishingService
from app.models_publication import PublicationRequest
from sqlalchemy import select
from app.database.models import Account, Media

Short = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_\-:.]+$')]
Body = Annotated[str, Field(min_length=1, max_length=10000)]
Limit = Annotated[int, Field(ge=1, le=100)]
Cursor = Annotated[str, Field(max_length=2048)]


def create_server(settings=None):
    settings = settings or Settings()
    TokenVault([settings.encryption_key.get_secret_value()])  # Fail at startup on invalid keys.
    engine, sessions = database(settings.database_url.get_secret_value())
    active = EffectiveSettings(settings, sessions)
    hub = Hub(sessions, active)
    publishing = PublishingService(active, sessions)
    @asynccontextmanager
    async def media_lifespan(_):
        from app.services.media_budget import MediaBudget
        async def janitor():
            while True:
                await asyncio.sleep(settings.media_cleanup_interval_seconds)
                try:
                    await asyncio.to_thread(MediaBudget(settings, sessions).cleanup)
                except Exception:
                    # Active transfer locks and transient storage/DB failures are retried next interval.
                    pass
        retained = (Path(settings.media_root) / '.retention-enabled').exists()
        task = asyncio.create_task(janitor()) if settings.media_ingestion_enabled or settings.media_attachment_probe_enabled or retained else None
        try:
            yield {}
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError): await task
    mcp = FastMCP('SocialHub', host='0.0.0.0', port=8000, stateless_http=True, json_response=False,
                  auth=AuthSettings(issuer_url=AnyHttpUrl(settings.oauth_issuer), resource_server_url=AnyHttpUrl(settings.public_url), required_scopes=['socialhub:read'], validate_token_resource=True),
                  token_verifier=Verifier(settings),
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                       allowed_hosts=list({urlsplit(settings.public_url).netloc, urlsplit(settings.browser_origin).netloc, 'localhost', '127.0.0.1', '127.0.0.1:8000', 'socialhub:8000'}),
                       allowed_origins=[settings.browser_origin, settings.public_url.rsplit('/mcp', 1)[0]]))
    # Apply the same exact boundary to custom browser/media/discovery routes.
    original_app = mcp.streamable_http_app
    def secured_app():
        app = original_app()
        sdk_lifespan = app.router.lifespan_context
        @asynccontextmanager
        async def managed_lifespan(application):
            async with sdk_lifespan(application) as state:
                async with media_lifespan(application):
                    yield state
        # Stateless MCP request lifespans end after each request; retention must
        # run for the ASGI application's entire lifetime, including idle periods.
        app.router.lifespan_context = managed_lifespan
        # AnyHttpUrl normalizes an issuer authority to a trailing slash. OIDC
        # issuer identifiers are exact strings; keep the SDK's native route and
        # challenge but preserve the operator's discovery-verified identifier.
        async def exact_metadata(request):
            return JSONResponse({'resource': settings.public_url,
                'authorization_servers': [settings.oauth_issuer],
                'scopes_supported': ['socialhub:read'], 'bearer_methods_supported': ['header']},
                headers={'Cache-Control': 'no-store'})
        for route in app.routes:
            if getattr(route, 'path', '') == '/.well-known/oauth-protected-resource/mcp':
                from mcp.server.auth.routes import cors_middleware
                route.endpoint = exact_metadata
                route.app = cors_middleware(exact_metadata, ['GET', 'OPTIONS'])
        app.add_middleware(OriginBoundary, settings=settings)
        return app
    mcp.streamable_http_app = secured_app
    log = logging.getLogger('socialhub')
    counters = {'success': 0, 'error': 0}

    async def invoke(operation, account_id=None, payload=None, idempotency_key=None, **args):
        token = get_access_token()
        subject = token.client_id if token else 'unauthenticated'
        try:
            if token is None:
                raise HubError('unauthenticated', 'Authenticated HTTP context required')
            if payload is not None:
                with sessions() as db:
                    live = db.get(Account, account_id)
                    is_live = live is not None and not live.is_mock
                required = 'socialhub:publish' if is_live else 'socialhub:mock-write'
                if required not in token.scopes:
                    raise HubError('insufficient_scope', 'The operation requires its write scope')
                if is_live and operation in {'publish_post', 'update_post', 'reply_to_comment'}:
                    if operation == 'reply_to_comment' and not payload.get('post_id'):
                        raise HubError('parent_post_required', 'Live replies require an owned parent post; use prepare_publication with post_id and comment_id')
                    request = PublicationRequest(account_ids=[account_id], operation={'publish_post': 'publish', 'update_post': 'update', 'reply_to_comment': 'reply'}[operation], **payload)
                    value = await publishing.prepare(subject, idempotency_key, request)
                    value['approval'] = publishing.approval_requirements(value['publication_id'], subject, token)
                else:
                    value = hub.write(operation, account_id, payload, idempotency_key, subject, token.scopes)
            else:
                value = await hub.read(operation, account_id, **args)
            hub.record(subject, operation, account_id, 'success')
            counters['success'] += 1
            log.info('mcp_operation', extra={'safe_fields': {'operation': operation, 'outcome': 'success'}})
            return {'ok': True, 'data': value}
        except HubError as error:
            hub.record(subject, operation, account_id, 'denied' if error.code in {'account_denied', 'insufficient_scope', 'unauthenticated', 'live_disabled'} else 'error', error.code, error.retryable)
            counters['error'] += 1
            log.info('mcp_operation', extra={'safe_fields': {'operation': operation, 'outcome': 'error', 'code': error.code}})
            return {'ok': False, 'error': {'code': error.code, 'message': error.message, 'retryable': error.retryable}}
        except Exception:
            counters['error'] += 1
            log.error('mcp_internal_error', extra={'safe_fields': {'operation': operation}})
            return {'ok': False, 'error': {'code': 'internal_error', 'message': 'Operation failed; inspect server health', 'retryable': False}}

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=settings.meta_live_reads or settings.admin_enabled)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=read, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read']}]})
    async def list_accounts() -> dict[str, Any]:
        """List authorized connected accounts; mock fixtures are hidden in live-read mode."""
        return await invoke('list_accounts')

    @mcp.tool(annotations=read, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read']}]})
    async def get_account_capabilities(account_id: Short) -> dict[str, Any]:
        """Return account capabilities and server publishing gates. Execution must also satisfy the reported approval policy."""
        return await invoke('get_account_capabilities', account_id)

    @mcp.tool(annotations=read, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read']}]})
    async def list_posts(account_id: Short, limit: Limit = 25, cursor: Cursor | None = None) -> dict[str, Any]:
        """Read posts from an allowlisted account with cursor pagination."""
        return await invoke('list_posts', account_id, limit=limit, cursor=cursor)

    @mcp.tool(annotations=read, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read']}]})
    async def get_post(account_id: Short, post_id: Short) -> dict[str, Any]:
        """Read a post belonging to the given account."""
        return await invoke('get_post', account_id, post_id=post_id)

    @mcp.tool(annotations=read, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read']}]})
    async def list_comments(account_id: Short, post_id: Short, limit: Limit = 25, cursor: Cursor | None = None) -> dict[str, Any]:
        """Read comments on a post belonging to the given account."""
        return await invoke('list_comments', account_id, post_id=post_id, limit=limit, cursor=cursor)

    @mcp.tool(annotations=read, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read']}]})
    async def get_system_status() -> dict[str, Any]:
        """Return database health and mock/live mode."""
        return await invoke('get_system_status')

    @mcp.tool(annotations=write, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read', 'socialhub:publish' if settings.meta_live_reads or settings.admin_enabled else 'socialhub:mock-write']}]})
    async def publish_post(account_id: Short, text: Body, idempotency_key: Short) -> dict[str, Any]:
        """Prepare an immutable text preview for review in ChatGPT. Follow the reported approval requirements before execute_publication; respect native write confirmation. Mock accounts retain simulation behavior."""
        return await invoke('publish_post', account_id, payload={'text': text}, idempotency_key=idempotency_key)

    @mcp.tool(annotations=write, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read', 'socialhub:publish' if settings.meta_live_reads or settings.admin_enabled else 'socialhub:mock-write']}]})
    async def update_post(account_id: Short, post_id: Short, text: Body, idempotency_key: Short) -> dict[str, Any]:
        """Prepare a supported Facebook text/link edit for review in ChatGPT. Follow its approval requirements before execution. Instagram caption replacement is unsupported."""
        return await invoke('update_post', account_id, payload={'post_id': post_id, 'text': text}, idempotency_key=idempotency_key)

    @mcp.tool(annotations=write, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read', 'socialhub:publish' if settings.meta_live_reads or settings.admin_enabled else 'socialhub:mock-write']}]})
    async def reply_to_comment(account_id: Short, comment_id: Short, text: Body, idempotency_key: Short, post_id: Short | None = None) -> dict[str, Any]:
        """Prepare a reply to an owned comment for review in ChatGPT under the reported approval policy. Live replies require the parent post ID; mocks retain their existing behavior."""
        return await invoke('reply_to_comment', account_id, payload={'comment_id': comment_id, 'text': text, **({'post_id': post_id} if post_id else {})}, idempotency_key=idempotency_key)

    @mcp.tool(annotations=write, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read', 'socialhub:publish' if settings.meta_live_reads or settings.admin_enabled else 'socialhub:mock-write']}]})
    async def schedule_post(account_id: Short, text: Body, run_at: Annotated[str, Field(max_length=64)], timezone: Annotated[str, Field(max_length=64)], idempotency_key: Short) -> dict[str, Any]:
        """MOCK ONLY. Persist a requested schedule with explicit offset and IANA timezone; no worker publishes it."""
        return await invoke('schedule_post', account_id, payload={'text': text, 'run_at': run_at, 'timezone': timezone}, idempotency_key=idempotency_key)

    async def publication_call(action, **arguments):
        token = get_access_token()
        try:
            if token is None:
                raise HubError('unauthenticated', 'Authenticated HTTP context required')
            if action == 'prepare' and 'socialhub:publish' not in token.scopes:
                raise HubError('insufficient_scope', 'Publication preparation requires publish scope')
            if action == 'prepare':
                value = await publishing.prepare(token.client_id, arguments['idempotency_key'], PublicationRequest.model_validate(arguments['request']))
            elif action == 'execute':
                value = await publishing.execute(token.client_id, token.scopes, arguments['publication_id'], context=token)
            elif action == 'requirements':
                value = publishing.approval_requirements(arguments['publication_id'], token.client_id, token)
            elif action == 'status':
                value = await publishing.refresh_status(arguments['publication_id'], token.client_id)
            else:
                value = publishing.review(arguments['publication_id'], token.client_id)
            if action != 'requirements':
                value['approval'] = publishing.approval_requirements(value['publication_id'], token.client_id, token)
                # Report the actual authenticated caller's policy, rather than
                # equating absence of a stored owner grant with a new CLI step.
                value['requires_owner_approval'] = value['approval']['needs_new_owner_grant']
            return {'ok': True, 'data': value}
        except HubError as error:
            if token:
                hub.record(token.client_id, action + '_publication', None, 'denied', error.code, error.retryable)
            return {'ok': False, 'error': {'code': error.code, 'message': error.message, 'retryable': error.retryable}}
        except ValueError:
            return {'ok': False, 'error': {'code': 'invalid_request', 'message': 'Publication fields are inconsistent or unsupported', 'retryable': False}}
        except Exception:
            log.error('publication_internal_error')
            return {'ok': False, 'error': {'code': 'internal_error', 'message': 'Publication operation failed; inspect safe status', 'retryable': False}}

    @mcp.tool(annotations=write, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read', 'socialhub:publish']}]})
    async def prepare_publication(account_ids: Annotated[list[Short], Field(min_length=1, max_length=8)], idempotency_key: Short,
                                  text: Annotated[str, Field(max_length=63206)] = '',
                                  content_type: Literal['text', 'image', 'link', 'video', 'reel'] = 'text',
                                  media_ids: Annotated[list[Short], Field(max_length=1)] = [], link_url: Annotated[str, Field(max_length=2048)] | None = None,
                                  operation: Literal['publish', 'update', 'reply'] = 'publish', post_id: Short | None = None, comment_id: Short | None = None,
                                  is_ai_generated: StrictBool | None = None) -> dict[str, Any]:
        """Prepare the exact requested content/accounts for review in ChatGPT. Does not publish or grant approval. For Reels, is_ai_generated defaults to the server preference (on by default); show its resolved value in the preview and use false only for non-AI footage. This disclosure is not publication consent. Immutable text, disclosure and media hashes bind execution; follow the returned approval path and respect native host confirmation."""
        return await publication_call('prepare', idempotency_key=idempotency_key, request={'account_ids': account_ids, 'text': text,
            'content_type': content_type, 'media_ids': media_ids, 'link_url': link_url, 'operation': operation, 'post_id': post_id, 'comment_id': comment_id,
            'is_ai_generated': is_ai_generated})

    @mcp.tool(annotations=read, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read']}]})
    async def get_publication_preview(publication_id: Short) -> dict[str, Any]:
        """Read your exact stored preview, content hash, approval requirement and independent platform statuses."""
        return await publication_call('review', publication_id=publication_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True), structured_output=True,
              meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read', 'socialhub:publish']}]})
    async def execute_publication(publication_id: Short) -> dict[str, Any]:
        """Execute only an explicitly requested and reviewed immutable publication. Respect native ChatGPT write confirmation. For Reels, each call advances at most one mutation per target; continue this same requested workflow after read-only status checks report a ready checkpoint. Observe retry_at. Never recreate or blindly retry uncertain targets. The configured policy enforces owner approval or authenticated client-policy reliance."""
        return await publication_call('execute', publication_id=publication_id)

    @mcp.tool(annotations=read, structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read']}]})
    async def get_publication_status(publication_id: Short) -> dict[str, Any]:
        """Read status and refresh acknowledged delivery metadata using Meta GETs only. Never publishes, resumes uploads or retries a mutation."""
        return await publication_call('status', publication_id=publication_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read']}]})
    async def get_approval_requirements(publication_id: Short) -> dict[str, Any]:
        """Explain the approval path for this exact preview and authenticated caller. Prefer review in ChatGPT and its native confirmation when supported. Does not grant approval or execute content."""
        return await publication_call('requirements', publication_id=publication_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False), structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read']}]})
    async def list_media(limit: Limit = 25) -> dict[str, Any]:
        """List private validated asset metadata. Upload bytes through the authenticated local media endpoint; attachments are not automatically available."""
        if get_access_token() is None:
            return {'ok': False, 'error': {'code': 'unauthenticated'}}
        with sessions() as db:
            from app.database.models import utcnow
            from sqlalchemy import or_
            rows = db.scalars(select(Media).where(
                or_(Media.expires_at.is_(None), Media.expires_at > utcnow()),
                Media.metadata_json['_bytes_deleted'].as_boolean().is_not(True)).execution_options(yield_per=32))
            assets = []
            for item in rows:
                if item.metadata_json.get('validated') and get_access_token().client_id in item.metadata_json.get('owners', []):
                    assets.append(publishing.media.describe(item))
                    if len(assets) >= limit: break
            return {'ok': True, 'data': {'media': assets}}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False), structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read']}]})
    async def get_media(media_id: Short) -> dict[str, Any]:
        """Read private media metadata; no signed download URL or storage path is exposed."""
        if get_access_token() is None:
            return {'ok': False, 'error': {'code': 'unauthenticated'}}
        try:
            return {'ok': True, 'data': publishing.media.get(media_id, get_access_token().client_id)}
        except HubError as error:
            return {'ok': False, 'error': {'code': error.code, 'message': error.message}}

    @mcp.custom_route('/healthz', methods=['GET'])
    async def health(request):
        return JSONResponse({'status': 'ok', 'mode': 'live_guarded_publishing' if settings.meta_live_writes and settings.publishing_enabled else 'live_read_only' if settings.meta_live_reads else 'mock'})

    @mcp.custom_route('/readyz', methods=['GET'])
    async def ready(request):
        try:
            with engine.connect() as db:
                db.execute(text('SELECT 1'))
                if db.execute(text('SELECT version_num FROM alembic_version')).scalar() != '0006':
                    return JSONResponse({'status': 'schema_mismatch'}, status_code=503)
            return JSONResponse({'status': 'ready'})
        except Exception:
            return JSONResponse({'status': 'unavailable'}, status_code=503)

    @mcp.custom_route('/metrics', methods=['GET'])
    async def metrics(request):
        token = request.headers.get('authorization', '').removeprefix('Bearer ')
        if await Verifier(settings).verify_token(token) is None:
            return PlainTextResponse('Unauthorized', status_code=401)
        return PlainTextResponse('\n'.join(f'socialhub_operations_total{{outcome="{key}"}} {value}' for key, value in counters.items()) + '\n')

    from app.security.media_routes import register_media_routes
    register_media_routes(mcp, settings, sessions)
    from app.security.attachment_tools import register_attachment_tools
    register_attachment_tools(mcp, settings, sessions)
    from app.security.meta_routes import register_meta_routes
    register_meta_routes(mcp, active, sessions)
    from app.security.approval_routes import register_approval_routes
    register_approval_routes(mcp, settings, sessions)
    from app.security.admin_routes import register_admin_routes
    register_admin_routes(mcp, settings, sessions)
    return mcp


if __name__ == '__main__':
    configure_logging()
    import uvicorn
    uvicorn.run(create_server().streamable_http_app(), host='0.0.0.0', port=8000, log_config=None, access_log=False, proxy_headers=False)
