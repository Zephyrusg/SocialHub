"""Upload a local file through authenticated Nginx. Does not publish anything."""
import argparse
from pathlib import Path
import httpx
from app.config import Settings

p = argparse.ArgumentParser()
p.add_argument('path', type=Path)
args = p.parse_args()
s = Settings()
if not args.path.is_file() or args.path.stat().st_size > s.media_max_bytes:
    raise SystemExit('Select an existing file within the configured size limit')
with args.path.open('rb') as data, httpx.Client(timeout=180, follow_redirects=False) as client:
    response = client.post(s.public_url.removesuffix('/mcp') + '/media/upload', content=data,
        headers={'Authorization': 'Bearer ' + s.local_token.get_secret_value(), 'Content-Type': 'application/octet-stream'})
    if response.status_code != 200:
        raise SystemExit('Media upload failed; verify format/size and the authenticated media scope')
    print(response.text)
