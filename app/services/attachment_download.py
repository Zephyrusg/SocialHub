"""Pull host-authorized file references without forwarding tokens or trusting DNS twice."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit
import httpx
from pydantic import BaseModel, ConfigDict, Field
from app.core import HubError


class ChatGPTFile(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    download_url: str = Field(repr=False)
    file_id: str = Field(repr=False)
    mime_type: str = ''
    file_name: str = ''


def attachment_host(reference):
    raw = reference.download_url
    try:
        u = urlsplit(raw)
        if (not 1 <= len(raw) <= 8192 or any(ord(c) < 33 or ord(c) == 127 for c in raw)
                or u.scheme != 'https' or not u.hostname or '@' in u.netloc
                or u.fragment or u.port not in {None, 443}
                or u.hostname != u.hostname.lower() or u.hostname.endswith('.')
                or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in u.hostname)
                or not 1 <= len(reference.file_id) <= 256
                or len(reference.mime_type) > 128 or len(reference.file_name) > 256):
            raise ValueError()
        try:
            ipaddress.ip_address(u.hostname)
        except ValueError:
            return u.hostname
        raise ValueError()
    except (ValueError, TypeError):
        raise HubError('invalid_attachment_reference', 'Use a host-provided HTTPS file reference without credentials, IP literals or fragments') from None


async def public_address(host):
    try:
        rows = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, 443,
            type=socket.SOCK_STREAM), timeout=5)
        addresses = [row[4][0] for row in rows]
        if not addresses or any(not public_ip(a) for a in addresses):
            raise ValueError()
        return next((a for a in addresses if ipaddress.ip_address(a).version == 4), addresses[0])
    except (OSError, ValueError, TimeoutError):
        raise HubError('attachment_host_denied', 'Attachment host must resolve only to public addresses') from None


def public_ip(value):
    try:
        address = ipaddress.ip_address(value)
        return address.is_global and not address.is_multicast and not address.is_reserved and '%' not in value
    except ValueError:
        return False


class AttachmentDownloader:
    def __init__(self, settings, transport=None, resolver=public_address):
        self.settings, self.transport, self.resolver = settings, transport, resolver

    async def receive(self, reference, output):
        host = attachment_host(reference)
        if host not in self.settings.media_download_hosts:
            raise HubError('attachment_host_unverified', 'The operator must verify and allow the exact host reported by the attachment probe')
        address = await self.resolver(host)
        if not public_ip(address):
            raise HubError('attachment_host_denied', 'Attachment destination is not public')
        # Pin the checked address while retaining certificate verification and SNI.
        url = httpx.URL(reference.download_url).copy_with(host=address)
        total = 0
        try:
            async with asyncio.timeout(self.settings.media_receive_timeout_seconds):
                async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                        follow_redirects=False, timeout=httpx.Timeout(15, connect=5)) as client:
                    async with client.stream('GET', url,
                            headers={'Host': host, 'Accept-Encoding': 'identity'},
                            extensions={'sni_hostname': host}) as response:
                        if response.status_code != 200:
                            raise HubError('attachment_download_failed', 'Attachment link expired, redirected or unavailable; attach the file again')
                        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                            raise HubError('invalid_media', 'Compressed transfer encodings are refused')
                        raw_length = response.headers.get('content-length')
                        if raw_length is not None and (len(raw_length) > 20 or not raw_length.isdecimal()
                                or not 0 < int(raw_length) <= self.settings.media_max_bytes):
                            raise HubError('invalid_media', 'Attachment exceeds the configured byte limit or has an invalid length')
                        async for chunk in response.aiter_raw(chunk_size=256 * 1024):
                            total += len(chunk)
                            if total > self.settings.media_max_bytes:
                                raise HubError('invalid_media', 'Attachment exceeds the configured byte limit')
                            await asyncio.to_thread(output.write, chunk)
                        if total == 0 or (raw_length is not None and total != int(raw_length)):
                            raise HubError('invalid_media', 'Attachment is empty or incomplete')
        except (httpx.HTTPError, TimeoutError, OSError):
            raise HubError('attachment_download_failed', 'Attachment transfer failed or timed out; no source URL or token is retained', True) from None
        return total
