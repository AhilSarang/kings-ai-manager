#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh.
#
#   scripts/restore.sh --verify BACKUP.dump          # safe drill: restore into a scratch DB, count rows, drop it
#   scripts/restore.sh BACKUP.dump [FILES.tar.gz]    # REAL restore: replaces the live database (asks first)
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_DIR=$(pwd)
env_get() { { grep -E "^$1=" .env 2>/dev/null || true; } | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
PGUSER=$(env_get POSTGRES_USER); PGUSER=${PGUSER:-kings}
PGDB=$(env_get POSTGRES_DB); PGDB=${PGDB:-kings_ai}
dc() { docker compose --project-directory "$PROJECT_DIR" "$@"; }
psql_db() { dc exec -T db psql -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$1" -tAc "$2"; }

verify=false
if [[ "${1:-}" == "--verify" ]]; then verify=true; shift; fi
dump=${1:?usage: restore.sh [--verify] BACKUP.dump [FILES.tar.gz]}
files=${2:-}
[[ -f "$dump" ]] || { echo "not found: $dump" >&2; exit 1; }

if $verify; then
  scratch="restore_check_$(date +%s)"
  echo "Restoring into scratch database $scratch ..."
  psql_db postgres "CREATE DATABASE $scratch"
  trap 'psql_db postgres "DROP DATABASE IF EXISTS $scratch WITH (FORCE)" >/dev/null' EXIT
  dc exec -T db pg_restore -U "$PGUSER" -d "$scratch" --no-owner --exit-on-error < "$dump"
  echo "Rows in the backup:"
  for t in users tasks task_events messages approvals memory_entries audit_log files; do
    printf '  %-15s %s\n' "$t" "$(psql_db "$scratch" "SELECT count(*) FROM $t")"
  done
  echo "Migration version: $(psql_db "$scratch" "SELECT version_num FROM alembic_version")"
  echo "Verify OK. The live database was not touched."
  exit 0
fi

cat <<MSG
This REPLACES the live database '$PGDB'${files:+ and the uploaded files} with:
  $dump ${files:+
  $files}
The api and worker will be stopped during the restore.
MSG
read -r -p "Type RESTORE to continue: " answer
[[ "$answer" == "RESTORE" ]] || { echo "Cancelled."; exit 1; }

echo "Taking a safety backup of the current state first..."
BACKUP_DIR="${BACKUP_DIR:-/var/backups/kings-ai}/pre-restore" "$PROJECT_DIR/scripts/backup.sh"

dc stop api worker
trap 'dc start api worker' EXIT
psql_db postgres "DROP DATABASE IF EXISTS $PGDB WITH (FORCE)"
psql_db postgres "CREATE DATABASE $PGDB OWNER $PGUSER"
dc exec -T db pg_restore -U "$PGUSER" -d "$PGDB" --no-owner --exit-on-error < "$dump"

if [[ -n "$files" ]]; then
  dc run --rm --no-deps -T --entrypoint sh -v "$(cd "$(dirname "$files")" && pwd)":/restore:ro api \
    -c "rm -rf /data/files/* && tar -C /data -xzf /restore/$(basename "$files")"
fi
echo "Restore complete. Starting services..."
