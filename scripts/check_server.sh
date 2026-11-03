#!/usr/bin/env bash
# Health report for the server (run on the server, e.g. after a reboot).
set -uo pipefail
cd "$(dirname "$0")/.."
env_get() { { grep -E "^$1=" .env 2>/dev/null || true; } | tail -1 | cut -d= -f2-; }
DOMAIN=$(env_get DOMAIN)
BACKUP_DIR=$(env_get BACKUP_DIR); BACKUP_DIR=${BACKUP_DIR:-/var/backups/kings-ai}
ok=true
check() { if eval "$2" >/dev/null 2>&1; then echo "OK    $1"; else echo "FAIL  $1"; ok=false; fi; }

echo "Uptime: $(uptime -p 2>/dev/null || uptime)"
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
check "boot service enabled"          "systemctl is-enabled kings-ai-manager.service"
check "backup timer active"           "systemctl is-active kings-ai-backup.timer"
check "API ready (db + worker)"       "curl -fsS http://127.0.0.1:8000/health/ready | grep -q '\"status\":\"ok\"'"
check "HTTPS https://$DOMAIN/health"  "curl -fsS https://$DOMAIN/health"
check "webhook rejects unsigned POST" "[ \$(curl -s -o /dev/null -w '%{http_code}' -X POST https://$DOMAIN/webhooks/whatsapp) = 403 ]"
check "backup in the last 26 hours"   "find '$BACKUP_DIR' -maxdepth 1 -name 'kings_ai-*.dump' -mmin -1560 | grep -q ."
check "disk under 85% full"           "[ \$(df --output=pcent / | tail -1 | tr -dc 0-9) -lt 85 ]"
echo "Pending/parked jobs: $(docker compose exec -T db psql -U "$(env_get POSTGRES_USER)" -d "$(env_get POSTGRES_DB)" -tAc \
  "select count(*) from jobs where status in ('pending','running','waiting_llm')" 2>/dev/null || echo '?')"
echo "Certificate: $(echo | openssl s_client -connect "$DOMAIN:443" -servername "$DOMAIN" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null || echo 'not available')"
$ok && echo "All checks passed." || { echo "Some checks failed."; exit 1; }
