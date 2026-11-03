#!/usr/bin/env bash
# Weekly malware scan of stored files (WhatsApp media, email attachments, backups).
# Alerts the owner on WhatsApp if anything is found; quiet otherwise.
set -euo pipefail

TARGETS=("$(grep -E '^FILE_STORAGE_DIR=' /opt/kings-ai-manager/.env | cut -d= -f2- || echo /data/files)")
[[ -d /var/backups/kings-ai ]] && TARGETS+=(/var/backups/kings-ai)
LOG=/var/log/kings-ai-clamscan.log
DOCKER_FILES=$(docker volume inspect kings-ai-manager_files -f '{{.Mountpoint}}' 2>/dev/null || true)
[[ -n "$DOCKER_FILES" && -d "$DOCKER_FILES" ]] && TARGETS=("$DOCKER_FILES" "${TARGETS[@]:1}")

echo "=== $(date -Is) scanning: ${TARGETS[*]}" >> "$LOG"
set +e
clamscan --recursive --infected --max-filesize=100M --max-scansize=500M "${TARGETS[@]}" >> "$LOG" 2>&1
code=$?
set -e

if [[ $code -eq 1 ]]; then
  found=$(grep -c "FOUND$" "$LOG" || true)
  cd /opt/kings-ai-manager
  docker compose exec -T api python scripts/notify_owner.py \
    "🦠 Malware scan found $found suspicious file(s) on the server. Nothing was deleted. See /var/log/kings-ai-clamscan.log and docs/INCIDENT_RESPONSE.md."
  exit 0   # alerting is the job; don't mark the unit failed
elif [[ $code -ne 0 ]]; then
  echo "clamscan error (exit $code)" >> "$LOG"
  exit $code
fi
echo "clean" >> "$LOG"
