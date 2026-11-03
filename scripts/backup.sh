#!/usr/bin/env bash
# Nightly backup: database (pg_dump custom format) + uploaded files.
# Keeps BACKUP_KEEP_DAYS (default 14) days. Optional off-server copy with rclone.
#
#   scripts/backup.sh                 # run from the project directory
#   BACKUP_DIR=/var/backups/kings-ai scripts/backup.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_DIR=$(pwd)
# Load only the variables we need from .env (never `source` the whole file).
env_get() { { grep -E "^$1=" .env 2>/dev/null || true; } | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }

BACKUP_DIR=${BACKUP_DIR:-$(env_get BACKUP_DIR)}
BACKUP_DIR=${BACKUP_DIR:-/var/backups/kings-ai}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-$(env_get BACKUP_KEEP_DAYS)}
KEEP_DAYS=${KEEP_DAYS:-14}
RCLONE_REMOTE=${BACKUP_RCLONE_REMOTE:-$(env_get BACKUP_RCLONE_REMOTE)}
PGUSER=$(env_get POSTGRES_USER); PGUSER=${PGUSER:-kings}
PGDB=$(env_get POSTGRES_DB); PGDB=${PGDB:-kings_ai}

stamp=$(date +%Y%m%d-%H%M%S)
umask 077
mkdir -p "$BACKUP_DIR"
log() { printf '%s backup: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }
fail() { log "FAILED: $*"; exit 1; }

db_file="$BACKUP_DIR/kings_ai-$stamp.dump"
files_file="$BACKUP_DIR/files-$stamp.tar.gz"

log "dumping database $PGDB"
docker compose --project-directory "$PROJECT_DIR" exec -T db \
  pg_dump -U "$PGUSER" -d "$PGDB" --format=custom --compress=6 > "$db_file.part" \
  || fail "pg_dump"
# Verify the dump is readable before accepting it.
docker compose --project-directory "$PROJECT_DIR" exec -T db pg_restore --list < "$db_file.part" > /dev/null \
  || fail "dump verification"
mv "$db_file.part" "$db_file"

log "archiving uploaded files"
docker compose --project-directory "$PROJECT_DIR" exec -T api \
  tar -C /data -czf - files > "$files_file.part" || fail "files archive"
tar -tzf "$files_file.part" > /dev/null || fail "files archive verification"
mv "$files_file.part" "$files_file"

log "wrote $(du -h "$db_file" | cut -f1) database + $(du -h "$files_file" | cut -f1) files"

# Retention: delete backups older than KEEP_DAYS days.
find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'kings_ai-*.dump' -o -name 'files-*.tar.gz' \) \
  -mtime +"$((KEEP_DAYS - 1))" -print -delete | sed 's/^/removed old backup: /'
find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.part' -mmin +60 -delete

if [[ -n "$RCLONE_REMOTE" ]]; then
  command -v rclone > /dev/null || fail "BACKUP_RCLONE_REMOTE is set but rclone is not installed"
  log "copying to $RCLONE_REMOTE"
  rclone copy "$BACKUP_DIR" "$RCLONE_REMOTE" --include 'kings_ai-*.dump' --include 'files-*.tar.gz' \
    || fail "off-server copy"
  rclone delete "$RCLONE_REMOTE" --min-age "${KEEP_DAYS}d" \
    --include 'kings_ai-*.dump' --include 'files-*.tar.gz' || log "warning: remote retention cleanup failed"
fi

date '+%Y-%m-%dT%H:%M:%S%z' > "$BACKUP_DIR/LAST_SUCCESS"
log "done"
