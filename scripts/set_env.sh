#!/usr/bin/env bash
# Set one secret in the server's .env without it appearing in argv, shell history or a file.
# Run it on the server, or from the Mac with a terminal attached:
#   ssh -t kings-ai "cd /opt/kings-ai-manager && scripts/set_env.sh AMAZON_LWA_REFRESH_TOKEN"
# The value is typed (hidden) or piped in, written to .env (0600), and the services are restarted.
set -euo pipefail

KEY=${1:-}
RESTART=${2:-restart}
[[ $KEY =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo "Usage: set_env.sh KEY_NAME [norestart]" >&2; exit 1; }

APP_DIR=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$APP_DIR/.env"
[[ -f $ENV_FILE ]] || { echo "No .env at $ENV_FILE" >&2; exit 1; }

if [[ -t 0 ]]; then
  read -r -s -p "Value for $KEY (hidden): " VALUE; echo
else
  VALUE=$(cat)            # piped in
fi
VALUE=${VALUE%$'\n'}
[[ -n $VALUE ]] || { echo "Empty value; nothing changed." >&2; exit 1; }

umask 077
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
grep -v "^${KEY}=" "$ENV_FILE" > "$TMP" || true
printf '%s=%s\n' "$KEY" "$VALUE" >> "$TMP"
cat "$TMP" > "$ENV_FILE"          # keep the original file's owner and permissions
echo "$KEY set (${#VALUE} characters). Not printed, not logged."

if [[ $RESTART != "norestart" ]]; then
  (cd "$APP_DIR" && docker compose up -d) >/dev/null
  echo "Services restarted."
fi
