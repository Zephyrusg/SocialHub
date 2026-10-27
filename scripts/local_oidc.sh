#!/usr/bin/env bash
# Operates only on project socialhub-oidc-test. Never calls SSH or Meta.
set -euo pipefail
cd "$(dirname "$0")/.."
export FONTCONFIG_FILE="$PWD/.tools/browser-libs/fonts.conf"
export LD_LIBRARY_PATH="$PWD/.tools/browser-libs/extracted/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
case "${1:-test}" in
  prepare)
    task_uv="${SOCIALHUB_UV:-$PWD/.tools/uv}"
    if [ ! -x .tools/oidc-venv/bin/python ]; then "$task_uv" venv .tools/oidc-venv; fi
    "$task_uv" pip install --python .tools/oidc-venv/bin/python -r deployment/local-oidc/requirements.txt
    .tools/oidc-venv/bin/python -m playwright install chromium
    mkdir -p .tools/browser-libs
    (
      cd .tools/browser-libs
      apt-get download libnspr4 libnss3 libatk1.0-0t64 libatk-bridge2.0-0t64 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libxkbcommon0 libasound2t64 libatspi2.0-0t64 libdrm2 libdrm-common libwayland-server0 libxi6 libxtst6 libxrender1 fontconfig-config fonts-dejavu-core
      for package in *.deb; do dpkg-deb -x "$package" extracted; done
    )
    printf '%s\n' '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">' "<fontconfig><dir>$PWD/.tools/browser-libs/extracted/usr/share/fonts</dir><cachedir>/tmp/socialhub-font-cache</cachedir></fontconfig>" > "$FONTCONFIG_FILE"
    .tools/oidc-venv/bin/python scripts/local_oidc_setup.py
    ;;
  up|test)
    .tools/oidc-venv/bin/python scripts/local_oidc_setup.py
    docker build --target runtime -t socialhub:phase3a1 .
    docker compose --project-name socialhub-oidc-test -f deployment/local-oidc/compose.yml up -d
    docker compose --project-name socialhub-oidc-test -f deployment/local-oidc/compose.yml up -d --wait --wait-timeout 45 authelia
    .tools/oidc-venv/bin/python scripts/local_oidc_totp.py
    docker compose --project-name socialhub-oidc-test -f deployment/local-oidc/compose.yml exec -T socialhub alembic upgrade head
    docker compose --project-name socialhub-oidc-test -f deployment/local-oidc/compose.yml exec -T socialhub python -c 'from app.config import Settings; from app.database import database; from app.services.hub import Hub; _,sessions=database(Settings().database_url.get_secret_value()); Hub(sessions).seed_mocks()'
    if [ "${1:-test}" = test ]; then .tools/oidc-venv/bin/python scripts/verify_local_oidc.py; fi
    ;;
  down)
    docker compose --project-name socialhub-oidc-test -f deployment/local-oidc/compose.yml down
    ;;
  *) printf '%s\n' 'Usage: scripts/local_oidc.sh prepare|up|test|down'; exit 2 ;;
esac
