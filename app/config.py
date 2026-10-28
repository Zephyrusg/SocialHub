from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr, PrivateAttr, model_validator
from urllib.parse import urlsplit
import ipaddress


def validate_url(value, *, https=True, path=None):
    u = urlsplit(value)
    if (u.scheme not in ({'https'} if https else {'http', 'https'}) or not u.hostname
            or u.username or u.password or u.query or u.fragment or '*' in value
            or (path is not None and u.path != path) or u.hostname != u.hostname.lower()):
        raise ValueError('An exact trusted URL without credentials/query/fragment is required')
    if u.scheme == 'http' and u.hostname not in {'localhost', '127.0.0.1'}:
        raise ValueError('HTTP is allowed only for loopback development')
    _ = u.port
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=('.env', 'secrets/meta.env'), extra='ignore', hide_input_in_errors=True)
    database_url: SecretStr
    encryption_key: SecretStr
    auth_mode: str = 'local'
    local_token: SecretStr = SecretStr('')
    oauth_issuer: str = 'https://identity.invalid/'
    oauth_audience: str = 'http://localhost:8787/mcp'
    oauth_jwks_url: str = ''
    public_url: str = 'http://localhost:8787/mcp'
    browser_origin: str = 'https://localhost:8788'
    trusted_proxy_cidrs: list[str] = ['127.0.0.1/32']
    oauth_allowed_subjects: list[str] = []
    oauth_allowed_client_ids: list[str] = []
    owner_oidc_enabled: bool = False
    owner_oidc_client_id: str = ''
    owner_oidc_client_secret: SecretStr = SecretStr('')
    owner_oidc_allowed_subjects: list[str] = []
    owner_oidc_require_mfa: bool = True
    admin_enabled: bool = False
    admin_oidc_client_id: str = ''
    admin_oidc_client_secret: SecretStr = SecretStr('')
    admin_allowed_subjects: list[str] = []
    publishing_allowed_subjects: list[str] = []
    _meta_configuration_revision: int | None = PrivateAttr(default=None)
    media_delivery_enabled: bool = True
    media_root: str = '/data/media'

    meta_app_id: str = ''
    meta_app_secret: SecretStr = SecretStr('')
    meta_graph_version: str = 'v26.0'
    meta_redirect_uri: str = 'https://localhost:8788/meta/oauth/callback'
    meta_instagram_account_type: Literal['creator', 'business'] = 'creator'
    meta_live_reads: bool = False
    meta_live_writes: bool = False
    meta_write_account_ids: list[Literal['pixelpapa-facebook', 'pixelpapa-instagram']] = []
    publishing_enabled: bool = False
    approval_mode: Literal['owner_required', 'chatgpt_native', 'chatgpt_direct'] = 'owner_required'
    chatgpt_confirmation_tested: bool = False
    chatgpt_confirmation_evidence_sha256: str = ''
    chatgpt_direct_tested: bool = False
    chatgpt_direct_evidence_sha256: str = ''
    chatgpt_residual_risk_accepted: bool = False
    chatgpt_allowed_client_ids: list[str] = []
    chatgpt_allowed_principals: list[str] = []
    chatgpt_allowed_operations: list[Literal['publish', 'update', 'reply']] = ['publish', 'reply']
    approval_portal_enabled: bool = False
    media_public_base_url: str = ''
    media_max_bytes: int = 100 * 1024 * 1024
    media_ingestion_enabled: bool = False
    media_attachment_probe_enabled: bool = False
    media_download_hosts: list[str] = []
    media_storage_max_bytes: int = 512 * 1024 * 1024
    media_storage_max_files: int = 16
    media_min_free_bytes: int = 64 * 1024 * 1024
    media_retention_seconds: int = 86400
    media_receive_timeout_seconds: int = 120
    media_cleanup_interval_seconds: int = 60
    media_stale_upload_seconds: int = 600
    media_image_max_pixels: int = 16_000_000
    facebook_media_publishing_enabled: bool = False
    facebook_media_allowed_types: list[Literal['image', 'video', 'reel']] = ['image', 'video']
    instagram_reels_publishing_enabled: bool = False
    reels_ai_generated_default: bool = True

    @model_validator(mode='after')
    def secure_defaults(self):
        validate_url(self.public_url, https=False, path='/mcp')
        validate_url(self.browser_origin, path='')
        for cidr in self.trusted_proxy_cidrs:
            ipaddress.ip_network(cidr, strict=True)
        if self.owner_oidc_enabled and not (self.approval_portal_enabled and self.owner_oidc_client_id
                and self.owner_oidc_client_secret.get_secret_value() and self.owner_oidc_allowed_subjects):
            raise ValueError('Owner OIDC requires a separate confidential client and exact owner subjects')
        if self.admin_enabled and not (self.admin_oidc_client_id and self.admin_oidc_client_secret.get_secret_value()
                and self.admin_allowed_subjects):
            raise ValueError('Administration requires its own confidential OIDC client and exact admin subjects')
        if self.admin_enabled and (self.admin_oidc_client_id in self.oauth_allowed_client_ids
                or self.admin_oidc_client_id == self.owner_oidc_client_id):
            raise ValueError('The administration client must be separate from MCP and approval clients')
        for subject in self.admin_allowed_subjects + self.publishing_allowed_subjects:
            if not isinstance(subject, str) or not 1 <= len(subject) <= 128 or subject != subject.strip():
                raise ValueError('Roles require bounded exact stable subjects')
        if self.auth_mode not in {'local', 'oauth'}:
            raise ValueError('auth_mode must be local or oauth')
        if self.auth_mode == 'local' and len(self.local_token.get_secret_value()) < 32:
            raise ValueError('A random local token of at least 32 characters is required')
        if self.auth_mode == 'oauth' and not (self.oauth_jwks_url.startswith('https://') and self.oauth_issuer.startswith('https://') and self.public_url.startswith('https://')):
            raise ValueError('OAuth requires HTTPS issuer, public URL and JWKS URL')
        if self.auth_mode == 'oauth' or self.owner_oidc_enabled or self.admin_enabled:
            validate_url(self.oauth_issuer)
            validate_url(self.oauth_jwks_url)
        if self.auth_mode == 'oauth' and self.oauth_audience != self.public_url:
            raise ValueError('OAuth audience must equal the exact MCP resource URL')
        if self.meta_graph_version != 'v26.0':
            raise ValueError('Phase 2A requires Graph API v26.0')
        if self.meta_live_writes and not self.publishing_enabled:
            raise ValueError('Phase 2A write guard: enable the approval-backed publishing service before live writes')
        if self.approval_mode in {'chatgpt_native', 'chatgpt_direct'}:
            import re
            tested = (self.chatgpt_confirmation_tested if self.approval_mode == 'chatgpt_native' else self.chatgpt_direct_tested)
            evidence = (self.chatgpt_confirmation_evidence_sha256 if self.approval_mode == 'chatgpt_native' else self.chatgpt_direct_evidence_sha256)
            if not (self.auth_mode == 'oauth' and tested
                    and self.chatgpt_residual_risk_accepted and self.chatgpt_allowed_client_ids
                    and self.chatgpt_allowed_principals and self.chatgpt_allowed_operations
                    and re.fullmatch(r'[a-f0-9]{64}', evidence)):
                raise ValueError('ChatGPT policy requires OAuth identities, recorded host tests and explicit operator risk acceptance')
            if any(not value.strip() or len(value) > 128 for value in self.chatgpt_allowed_client_ids + self.chatgpt_allowed_principals):
                raise ValueError('ChatGPT identity allowlists require bounded nonempty exact identifiers')
        if self.media_public_base_url:
            from urllib.parse import urlparse
            u = urlparse(self.media_public_base_url)
            if u.scheme != 'https' or not u.netloc or u.username or u.password or u.query or u.fragment:
                raise ValueError('Media delivery requires an explicit HTTPS base URL without credentials/query')
        if not 1 <= self.media_max_bytes <= 512 * 1024 * 1024:
            raise ValueError('Invalid media size limit')
        if (not self.media_max_bytes <= self.media_storage_max_bytes <= 10 * 1024**3
                or not 1 <= self.media_storage_max_files <= 256
                or not 0 <= self.media_min_free_bytes <= 1024**3
                or not 3600 <= self.media_retention_seconds <= 172800
                or not 1 <= self.media_receive_timeout_seconds <= 600
                or not 10 <= self.media_cleanup_interval_seconds <= 3600
                or not 60 <= self.media_stale_upload_seconds <= 3600
                or not 1 <= self.media_image_max_pixels <= 20_000_000):
            raise ValueError('Invalid bounded media resource settings')
        for host in self.media_download_hosts:
            if (not host or host != host.lower() or host.endswith('.') or '*' in host
                    or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in host)):
                raise ValueError('Media download hosts must be exact lowercase DNS names')
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError('Media download hosts cannot be IP literals')
        if self.meta_redirect_uri != self.browser_origin + '/meta/oauth/callback':
            raise ValueError('Meta callback must exactly match the trusted browser origin and native callback path')
        return self
