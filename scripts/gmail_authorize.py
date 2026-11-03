"""Connect Gmail (one-time, run on the owner's Mac).

  uv run python scripts/gmail_authorize.py --client-file ~/Downloads/client_secret_XXXX.json --server kings-ai

What it does:
1. Installs the OAuth client id/secret on the server's .env (and a TOKEN_ENCRYPTION_KEY if missing),
   passing values over SSH stdin so they never appear in command lines or logs; restarts api/worker.
2. Opens Google's consent page in your browser (sign in as the monitored mailbox and click Allow;
   Google will warn that the app is unverified: Advanced -> Go to Kings Distro AI Manager).
3. Checks the signed-in mailbox is the expected one, then sends the refresh token to the server over
   SSH stdin, where it is stored encrypted and Gmail syncing starts.

Local development: --local stores the token in the local database instead of the server.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose",
          # Phase 3: read listing images the owner shares, and write FBA labels back to a Drive folder
          "https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive.file"]
APP_DIR = "/opt/kings-ai-manager"

REMOTE_ENV_EDITOR = r'''
import json, os, sys, stat
from pathlib import Path
updates = json.loads(sys.stdin.read())
p = Path(sys.argv[1])
lines = p.read_text().splitlines() if p.exists() else []
keys = {l.split("=", 1)[0] for l in lines if "=" in l and not l.lstrip().startswith("#")}
current = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in lines if "=" in l and not l.lstrip().startswith("#")}
if updates.get("TOKEN_ENCRYPTION_KEY") == "__generate_if_missing__":
    if current.get("TOKEN_ENCRYPTION_KEY"):
        updates.pop("TOKEN_ENCRYPTION_KEY")
    else:
        import base64
        updates["TOKEN_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode()
out = []
for l in lines:
    k = l.split("=", 1)[0] if "=" in l and not l.lstrip().startswith("#") else None
    if k in updates:
        out.append(f"{k}={updates.pop(k)}")
    else:
        out.append(l)
out += [f"{k}={v}" for k, v in updates.items()]
p.write_text("\n".join(out) + "\n")
os.chmod(p, 0o600)
print("env updated")
'''


def update_env_text(text: str, updates: dict[str, str]) -> str:
    """Pure version of REMOTE_ENV_EDITOR used by tests (without key generation)."""
    updates = dict(updates)
    out = []
    for line in text.splitlines():
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
        out.append(f"{key}={updates.pop(key)}" if key in updates else line)
    out += [f"{k}={v}" for k, v in updates.items()]
    return "\n".join(out) + "\n"


def read_client(path: str) -> tuple[str, str]:
    data = json.loads(Path(path).expanduser().read_text())
    section = data.get("installed") or data.get("web")
    if not section:
        sys.exit("That file is not a Google OAuth client JSON")
    return section["client_id"], section["client_secret"]


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def ssh(server: str, command: str, stdin: str) -> str:
    r = subprocess.run(["ssh", server, command], input=stdin, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        sys.exit(f"Server command failed: {r.stderr.strip()[:300]}")
    return r.stdout


def client_from_server(server: str) -> tuple[str, str]:
    """Read the client id/secret already stored in the server's .env (never printed)."""
    out = ssh(server, f"grep -E '^GOOGLE_OAUTH_CLIENT_(ID|SECRET)=' {APP_DIR}/.env", "")
    values = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    client_id = values.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = values.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        sys.exit(f"{server} has no Google OAuth client configured; pass --client-file")
    print(f"Using the OAuth client already on {server} ({client_id[:18]}…).")
    return client_id, client_secret


def install_client(server: str, client_id: str, client_secret: str) -> None:
    payload = json.dumps({"GOOGLE_OAUTH_CLIENT_ID": client_id, "GOOGLE_OAUTH_CLIENT_SECRET": client_secret,
                          "GMAIL_BACKEND": "api", "TOKEN_ENCRYPTION_KEY": "__generate_if_missing__"})
    script = base64.b64encode(REMOTE_ENV_EDITOR.encode()).decode()
    print(ssh(server, f"python3 -c \"import base64;exec(base64.b64decode('{script}'))\" {APP_DIR}/.env", payload).strip())
    print("Restarting api and worker ...")
    ssh(server, f"cd {APP_DIR} && docker compose up -d api worker >/dev/null 2>&1 && echo restarted", "")


def run_consent(client_id: str, client_secret: str, login_hint: str) -> tuple[str, str]:
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"State mismatch")
                return
            result.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Gmail connected. You can close this tab and return to the terminal.</h2>")

        def log_message(self, *args):  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "login_hint": login_hint,
        "include_granted_scopes": "false",
    })
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    print(f"\nOpening Google in your browser. Sign in as {login_hint} and click Allow.")
    print("If it warns the app is unverified: Advanced -> Go to Kings Distro AI Manager (unsafe).")
    print(f"If the browser doesn't open, visit:\n{url}\n")
    webbrowser.open(url)
    thread.join(timeout=600)
    server.server_close()
    if "error" in result:
        sys.exit(f"Google returned an error: {result['error']}")
    if "code" not in result:
        sys.exit("No response from Google (timed out after 10 minutes)")
    granted = set(result.get("scope", "").split())
    if not set(SCOPES) <= granted:
        sys.exit("Both Gmail permissions are needed; please tick every box on the consent screen and try again")
    r = httpx.post(TOKEN_URL, data={
        "code": result["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
    }, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Token exchange failed: {r.json().get('error', r.status_code)}")
    tokens = r.json()
    if "refresh_token" not in tokens:
        sys.exit("Google did not return a refresh token; remove the app's access at "
                 "myaccount.google.com/permissions and run this again")
    return tokens["refresh_token"], tokens["access_token"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-file", help="OAuth client JSON from Google Cloud "
                                         "(omit to reuse the credentials already on the server)")
    ap.add_argument("--server", default="kings-ai", help="SSH host of the production server")
    ap.add_argument("--account", default="you@example.com")
    ap.add_argument("--skip-install", action="store_true", help="client id/secret already on the server")
    ap.add_argument("--local", action="store_true", help="store the token in the local dev database instead")
    args = ap.parse_args()

    if args.client_file:
        client_id, client_secret = read_client(args.client_file)
    elif args.local:
        sys.exit("--local needs --client-file")
    else:
        client_id, client_secret = client_from_server(args.server)  # re-authorising: the file may be long gone
        args.skip_install = True
    if not args.local and not args.skip_install:
        install_client(args.server, client_id, client_secret)

    refresh_token, access_token = run_consent(client_id, client_secret, args.account)
    profile = httpx.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                        headers={"Authorization": f"Bearer {access_token}"}, timeout=30).json()
    email = (profile.get("emailAddress") or "").lower()
    if email != args.account.lower():
        sys.exit(f"You signed in as {email or 'an unknown account'}, not {args.account}. Nothing was stored.")
    print(f"Signed in as {email}.")

    if args.local:
        from app.db.session import session_scope
        from scripts.gmail_store_token import store

        with session_scope() as s:
            print(store(s, refresh_token))
    else:
        out = ssh(args.server, f"cd {APP_DIR} && docker compose exec -T api python scripts/gmail_store_token.py",
                  refresh_token)
        print(out.strip())
    print("Done. Gmail is connected; the first import runs in the background and you'll get a WhatsApp summary.")


if __name__ == "__main__":
    main()
