"""Run locally in a terminal. Never paste credentials into the conversation."""
import getpass
import os
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1] / 'secrets'
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / 'meta.env'
    if path.exists():
        raise SystemExit('secrets/meta.env already exists; edit it securely rather than overwrite it')
    app_id = input('Meta App ID (SocialHub MCP): ').strip()
    secret = getpass.getpass('Meta App Secret (hidden): ').strip()
    if not app_id.isdigit() or not secret or any(c in secret for c in '\r\n\"\''):
        raise SystemExit('Invalid App ID or secret format')
    content = f'META_APP_ID={app_id}\nMETA_APP_SECRET={secret}\nMETA_REDIRECT_URI=https://localhost:8788/meta/oauth/callback\nMETA_LIVE_READS=true\nMETA_LIVE_WRITES=false\n'
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    print('Saved secrets/meta.env with mode 0600. Credentials were not printed. Restart the application after configuration.')


if __name__ == '__main__':
    main()
