#!/bin/sh
# SQLite 온라인 백업(WAL 모드라 파일을 그냥 복사하면 안 된다). 14일 지난 백업은 삭제한다.
#   cbt 사용자의 cron:  30 2 * * *  /opt/ogg-cbt/app/deploy/backup.sh
# 백업 디렉터리는 미리 만들어 둔다: sudo install -d -o cbt -g cbt -m 700 /var/backups/ogg-cbt
set -eu
DB="${DB:-/var/lib/ogg-cbt/cbt.db}"
DEST="${DEST:-/var/backups/ogg-cbt}"
VENV="${VENV:-/opt/ogg-cbt/.venv}"
KEEP_DAYS="${KEEP_DAYS:-14}"
umask 077
OUT="$DEST/cbt-$(date +%F_%H%M).db"
"$VENV/bin/python" - "$DB" "$OUT" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
PY
find "$DEST" -name 'cbt-*.db' -mtime +"$KEEP_DAYS" -delete
echo "backup: $OUT"
