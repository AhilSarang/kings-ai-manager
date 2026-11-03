#!/usr/bin/env bash
# One-time: connect nightly backups to a Cloudflare R2 bucket. Run on the server:
#   /opt/kings-ai-manager/scripts/setup_r2.sh
# Asks for the R2 Access Key ID and Secret Access Key (input hidden), stores them
# only in /root/.config/rclone/rclone.conf (0600), runs a backup, and checks the copy.
set -euo pipefail

ACCOUNT_ID=${R2_ACCOUNT_ID:-8610cc4ec8797ac4e2672837d4aa849b}
BUCKET=${R2_BUCKET:-kings-ai-backups}
REMOTE="r2:$BUCKET"
APP_DIR=$(cd "$(dirname "$0")/.." && pwd)

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
command -v rclone >/dev/null || { apt-get install -y -q rclone >/dev/null; }

read -r -p "R2 Access Key ID: " KEY_ID
read -r -s -p "R2 Secret Access Key (hidden): " SECRET; echo
[[ -n "$KEY_ID" && -n "$SECRET" ]] || { echo "Both values are required." >&2; exit 1; }

umask 077
mkdir -p /root/.config/rclone
conf=/root/.config/rclone/rclone.conf
# Replace any previous [r2] section, keep other remotes.
if [[ -f $conf ]]; then
  awk 'BEGIN{skip=0} /^\[/{skip=($0=="[r2]")} !skip' "$conf" > "$conf.tmp" && mv "$conf.tmp" "$conf"
fi
cat >> "$conf" <<CONF
[r2]
type = s3
provider = Cloudflare
access_key_id = $KEY_ID
secret_access_key = $SECRET
endpoint = https://$ACCOUNT_ID.r2.cloudflarestorage.com
no_check_bucket = true
# R2 has no object versioning; skip rclone's post-upload HEAD, which R2 answers with 501.
no_head = true
CONF
chmod 600 "$conf"
unset SECRET

echo "Checking access to $REMOTE ..."
rclone lsf "$REMOTE" --max-depth 1 >/dev/null || { echo "Cannot reach the bucket. Check the key and its bucket permission." >&2; exit 1; }

if grep -q '^BACKUP_RCLONE_REMOTE=' "$APP_DIR/.env"; then
  sed -i "s|^BACKUP_RCLONE_REMOTE=.*|BACKUP_RCLONE_REMOTE=$REMOTE|" "$APP_DIR/.env"
else
  echo "BACKUP_RCLONE_REMOTE=$REMOTE" >> "$APP_DIR/.env"
fi

echo "Running a backup now ..."
systemctl start kings-ai-backup.service
journalctl -u kings-ai-backup -n 3 --no-pager | cut -c1-150
echo "Files in $REMOTE:"
rclone lsf "$REMOTE" | tail -5
echo "R2 backups are set up."
