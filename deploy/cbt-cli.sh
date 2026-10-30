#!/bin/sh
# 서버에서 관리 CLI 를 실행하는 도우미. 서비스와 같은 설정(/etc/ogg-cbt/env)을 읽는다.
#   sudo -u cbt /opt/ogg-cbt/app/deploy/cbt-cli.sh create-admin admin "관리자"
#   sudo -u cbt /opt/ogg-cbt/app/deploy/cbt-cli.sh reset-password admin
set -eu
APP_DIR="${APP_DIR:-/opt/ogg-cbt/app}"
VENV="${VENV:-/opt/ogg-cbt/.venv}"
ENV_FILE="${ENV_FILE:-/etc/ogg-cbt/env}"
set -a
. "$ENV_FILE"
set +a
cd "$APP_DIR"
exec "$VENV/bin/python" -m app.cli "$@"
