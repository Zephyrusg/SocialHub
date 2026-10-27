# Local development

Use Python 3.12+ and uv:

```bash
uv sync --locked --extra test
uv run --locked --extra test pytest -q
```

The tests use temporary databases, generated synthetic signing keys, fake HTTP transports and tiny committed image/video fixtures. Normal tests do not encode media or contact real Meta accounts. Host video checks may skip without ffprobe; use the Docker test target for the bundled validator.

The local Docker example expects an existing database and external `socialhub_local` network. Create a dedicated development schema/user through your own secure DB administration. Copy `.env.example` into an ignored `.env`, replace placeholders with independently generated secrets, and configure the exact local network/proxy addresses. Never use real production/root DB credentials as application credentials. The old operator-specific provisioning script is deliberately absent from the public source.

`scripts/local_tls.py` creates a local development CA and localhost certificate under ignored `secrets/tls/`. It does not install trust in your browser. Do not expose the local static bearer endpoint beyond loopback.

## Optional isolated OIDC harness

The browser/identity fixture is separate from normal tests and runtime dependencies. It needs Docker, a compatible local environment, test dependencies from `deployment/local-oidc/requirements.txt`, Chromium and its platform libraries. `scripts/local_oidc.sh` documents the WSL/Debian preparation sequence; review it before running it on a different OS. Set `SOCIALHUB_UV` to your uv executable if it is not installed at `.tools/uv`.

The fixture uses reserved `.test` domains, synthetic users, randomly generated secrets, loopback listeners, a separate Compose project/network and disposable data. The helpers perform no SSH access and do not configure any real production service. They can build/restart only the explicitly named test project. Run them with ordinary Python, not optimization (`-O`), so verification assertions remain active.

Fixture credentials, TOTP seeds, certificates and test databases are generated locally and must never be committed. Browser screenshots/traces and full OAuth URLs can leak sensitive values; keep them out of diagnostics. The fixture proves protocol integration, not an actual production owner's identity or consent.
