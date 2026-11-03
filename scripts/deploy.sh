#!/usr/bin/env bash
# Deploy or update the server from this Mac.
#
#   scripts/deploy.sh root@SERVER_IP --first   # first time: also uploads .env.server as .env and installs services
#   scripts/deploy.sh root@SERVER_IP           # later updates: code only
#
# The server must have been prepared with deploy/setup_server.sh.
set -euo pipefail

TARGET=${1:?usage: deploy.sh user@host [--first]}
FIRST=false
[[ "${2:-}" == "--first" ]] && FIRST=true
APP_DIR=/opt/kings-ai-manager
cd "$(dirname "$0")/.."

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Note: deploying uncommitted changes." >&2
fi
if $FIRST && [[ ! -f .env.server ]]; then
  echo "Create .env.server first: uv run python scripts/make_server_env.py --domain ... --email ..." >&2
  exit 1
fi

CADDY_BEFORE=$(ssh "$TARGET" "sha256sum $APP_DIR/ops/Caddyfile 2>/dev/null | cut -d' ' -f1" || true)
echo "==> Uploading code to $TARGET:$APP_DIR"
rsync -az --delete --exclude-from deploy/rsync-exclude.txt ./ "$TARGET:$APP_DIR/"

if $FIRST; then
  echo "==> Uploading server .env"
  if ssh "$TARGET" "test -f $APP_DIR/.env"; then
    echo "The server already has .env; leaving it unchanged." >&2
  else
    scp -q .env.server "$TARGET:$APP_DIR/.env"
    ssh "$TARGET" "chmod 600 $APP_DIR/.env"
  fi
fi

echo "==> Building and starting on the server"
ssh "$TARGET" FIRST="$FIRST" APP_DIR="$APP_DIR" CADDY_BEFORE="$CADDY_BEFORE" bash -s <<'REMOTE'
set -euo pipefail
cd "$APP_DIR"
chmod +x scripts/*.sh deploy/*.sh
grep -q '^COMPOSE_FILE=' .env || { echo ".env is missing COMPOSE_FILE (use scripts/make_server_env.py)" >&2; exit 1; }
if [[ "$FIRST" == "true" ]]; then
  install -m 0644 deploy/kings-ai-manager.service deploy/kings-ai-backup.service \
    deploy/kings-ai-backup.timer 'deploy/kings-ai-alert@.service' /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable kings-ai-manager.service kings-ai-backup.timer
  systemctl start kings-ai-backup.timer
fi
docker compose build --pull
docker compose up -d --remove-orphans
if [[ -n "$CADDY_BEFORE" && "$(sha256sum ops/Caddyfile | cut -d' ' -f1)" != "$CADDY_BEFORE" ]]; then
  echo "Caddyfile changed; restarting Caddy"
  docker compose restart caddy
fi
echo "Waiting for the API..."
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then break; fi
  sleep 2
done
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
docker image prune -f >/dev/null
REMOTE

echo "==> Checking from here"
DOMAIN=$(ssh "$TARGET" "grep -E '^DOMAIN=' $APP_DIR/.env | cut -d= -f2-")
for _ in $(seq 1 30); do
  # Ask public DNS directly; the Mac's own resolver may still cache an old answer.
  IP=$(dig +short +time=2 +tries=1 "$DOMAIN" @1.1.1.1 2>/dev/null | grep -E "^[0-9.]+$" | tail -1 || true)
  [[ -n "$IP" ]] || IP=$(ssh -G "$TARGET" | awk '/^hostname /{print $2}')  # DNS blocked here: use the SSH host
  if [[ -n "$IP" ]] && curl -fsS --resolve "$DOMAIN:443:$IP" "https://$DOMAIN/health" 2>/dev/null; then
    echo; echo "Deployed: https://$DOMAIN"; exit 0
  fi
  sleep 5
done
echo "HTTPS isn't answering yet. Check that $DOMAIN points to the server, then run on the server:" >&2
echo "  cd $APP_DIR && docker compose logs caddy --tail 50" >&2
exit 1
