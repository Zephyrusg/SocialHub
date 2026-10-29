"""Host file parameters are explicit inputs; attachments are never inferred."""
import logging
from typing import Any, Annotated
from pydantic import Field
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import ToolAnnotations
from app.core import HubError
from app.services.administration import require_publisher
from app.services.attachment_download import ChatGPTFile
from app.services.media_ingestion import MediaIngestion
from app.services.media_normalization import MediaNormalization


def register_attachment_tools(mcp, settings, sessions):
    ingestion = MediaIngestion(settings, sessions)
    normalization = MediaNormalization(settings, sessions)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
              structured_output=True, meta={'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read', 'socialhub:publish']}]})
    async def normalize_media(
            media_id: Annotated[str, Field(min_length=36, max_length=36, pattern=r'^[a-f0-9-]{36}$')],
            idempotency_key: Annotated[str, Field(min_length=1, max_length=128)]) -> dict[str, Any]:
        """Explicitly create an Instagram-compatible copy of an owned imported MP4. Requires the administrator's Instagram Media Normalization switch. Preserves video, may re-encode AAC audio, removes edit lists, validates a new expiring asset. Never modifies the original or publishes. Use the NEW media ID in a NEW prepare_publication preview, retain the requested caption/AI disclosure, review timing, and wait for an explicit publication instruction. Reuse the same idempotency key for the same source/profile; never use this to retry an existing publication."""
        token = get_access_token()
        try:
            if token is None:raise HubError('unauthenticated', 'Authenticated MCP context required')
            if 'socialhub:publish' not in token.scopes:raise HubError('insufficient_scope', 'Media conversion requires publish scope')
            require_publisher(settings, token.client_id)
            return {'ok': True, 'data': await normalization.normalize(media_id, token.client_id, idempotency_key)}
        except HubError as error:
            return {'ok': False, 'error': {'code': error.code, 'message': error.message, 'retryable': error.retryable}}
        except Exception:
            logging.getLogger('socialhub').error('media_normalization_error')
            return {'ok': False, 'error': {'code': 'media_normalization_failed', 'message': 'Private conversion failed; original unchanged and no publication attempted', 'retryable': False}}

    async def invoke(file, *, key=None, probe=False):
        token = get_access_token()
        try:
            if token is None:
                raise HubError('unauthenticated', 'Authenticated MCP context required')
            if not probe:
                if 'socialhub:publish' not in token.scopes:
                    raise HubError('insufficient_scope', 'Media import requires publish scope')
                require_publisher(settings, token.client_id)
                result = await ingestion.import_file(file, token.client_id, key)
            else:
                result = await ingestion.probe(file)
            return {'ok': True, 'data': result}
        except HubError as error:
            return {'ok': False, 'error': {'code': error.code, 'message': error.message, 'retryable': error.retryable}}
        except Exception:
            logging.getLogger('socialhub').error('media_ingestion_error')
            return {'ok': False, 'error': {'code': 'media_ingestion_failed', 'message': 'Private media validation or storage failed; no publication was attempted', 'retryable': False}}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True),
              structured_output=True, meta={'openai/fileParams': ['file'],
                  'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read']}]})
    async def probe_media_attachment(file: ChatGPTFile) -> dict[str, Any]:
        """Test an actual ChatGPT attachment reference. Reports only the hostname when downloading is disabled; optional bounded validation deletes temporary bytes and never publishes or registers an asset. Never invent a download URL or use a local sandbox path."""
        return await invoke(file, probe=True)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
              structured_output=True, meta={'openai/fileParams': ['file'],
                  'securitySchemes': [{'type': 'oauth2', 'scopes': ['socialhub:read', 'socialhub:publish']}]})
    async def import_media(file: ChatGPTFile, idempotency_key: str) -> dict[str, Any]:
        """Import a user-supplied ChatGPT image/MP4 attachment into private expiring storage, without publishing. Pass the returned media ID to prepare_publication with image/video/reel content type; one Reel asset can target both platforms. Use actual host file parameters, never guessed URLs or sandbox paths. A separate explicit user publication instruction is required."""
        return await invoke(file, key=idempotency_key)

    # SDK validation errors must not echo a capability URL from malformed input.
    # Pinned MCP v1 exposes the registered argument model through ToolManager.
    for name in ['probe_media_attachment', 'import_media', 'normalize_media']:
        tool = mcp._tool_manager.get_tool(name)
        tool.fn_metadata.arg_model.model_config['hide_input_in_errors'] = True
        tool.fn_metadata.arg_model.model_rebuild(force=True)
