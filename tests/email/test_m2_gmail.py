import json

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Job
from app.email import accounts, crypto
from app.email.client import get_client, handle_auth_failure, store_refresh_token
from app.email.fake_gmail import FakeGmailClient, parse_raw
from app.email.gmail import (
    GmailAuthError,
    GmailError,
    GmailHistoryGone,
    GmailRetryable,
    HttpGmailClient,
    build_raw,
)
from scripts.gmail_authorize import pkce_pair, update_env_text
from scripts.gmail_store_token import store
from tests.conftest import run_jobs


# ---------- crypto ----------
def test_token_encryption_roundtrip():
    enc = crypto.encrypt("1//refresh-token")
    assert "refresh" not in enc and crypto.decrypt(enc) == "1//refresh-token"


def test_token_encryption_bad_key(monkeypatch):
    from pydantic import SecretStr

    enc = crypto.encrypt("x")
    monkeypatch.setattr(get_settings(), "token_encryption_key", SecretStr(crypto.new_key()))
    with pytest.raises(crypto.TokenCryptoError):
        crypto.decrypt(enc)
    monkeypatch.setattr(get_settings(), "token_encryption_key", SecretStr(""))
    with pytest.raises(crypto.TokenCryptoError):
        crypto.encrypt("x")


# ---------- HTTP client ----------
class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        resp = self.responses.pop(0)
        return resp(request) if callable(resp) else resp


def token_ok(expires=3600):
    return httpx.Response(200, json={"access_token": "AT", "expires_in": expires})


def client(responses):
    rec = Recorder(responses)
    c = HttpGmailClient("RT", client_id="cid", client_secret="sec", http=httpx.Client(transport=httpx.MockTransport(rec)))
    return c, rec


def test_refresh_then_call_and_token_cached():
    c, rec = client([token_ok(), httpx.Response(200, json={"emailAddress": "a@b.com"}),
                     httpx.Response(200, json={"emailAddress": "a@b.com"})])
    assert c.get_profile()["emailAddress"] == "a@b.com"
    c.get_profile()
    assert [r.url.path for r in rec.requests] == ["/token", "/gmail/v1/users/me/profile", "/gmail/v1/users/me/profile"]
    assert rec.requests[1].headers["authorization"] == "Bearer AT"
    body = dict(x.split("=") for x in rec.requests[0].content.decode().split("&"))
    assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "RT"


def test_401_refreshes_once_then_succeeds():
    c, rec = client([token_ok(), httpx.Response(401), token_ok(), httpx.Response(200, json={"ok": 1})])
    assert c.get_profile() == {"ok": 1}
    assert len(rec.requests) == 4


def test_invalid_grant_is_auth_error():
    c, _ = client([httpx.Response(400, json={"error": "invalid_grant"})])
    with pytest.raises(GmailAuthError, match="invalid_grant"):
        c.get_profile()


def test_missing_client_config_is_auth_error():
    c = HttpGmailClient("RT", client_id="", client_secret="", http=httpx.Client())
    c.client_id = c.client_secret = ""
    with pytest.raises(GmailAuthError):
        c.get_profile()


@pytest.mark.parametrize("resp,exc", [
    (httpx.Response(429, headers={"retry-after": "7"}), GmailRetryable),
    (httpx.Response(503), GmailRetryable),
    (httpx.Response(403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}), GmailRetryable),
    (httpx.Response(403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}}), GmailAuthError),
    (httpx.Response(400, json={"error": {"errors": [{"reason": "invalidArgument"}]}}), GmailError),
])
def test_error_mapping(resp, exc):
    c, _ = client([token_ok(), resp])
    with pytest.raises(exc):
        c.get_message("m1")


def test_retry_after_parsed():
    c, _ = client([token_ok(), httpx.Response(429, headers={"retry-after": "7"})])
    with pytest.raises(GmailRetryable) as e:
        c.get_message("m1")
    assert e.value.retry_after == 7


def test_history_404_is_history_gone():
    c, rec = client([token_ok(), httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})])
    with pytest.raises(GmailHistoryGone):
        c.list_history(5)
    assert rec.requests[1].url.params["startHistoryId"] == "5"
    assert rec.requests[1].url.params["historyTypes"] == "messageAdded"


def test_network_error_is_retryable():
    def boom(request):
        raise httpx.ConnectError("down")

    c, _ = client([boom])
    with pytest.raises(GmailRetryable):
        c.get_profile()


def test_watch_and_send_request_shapes():
    c, rec = client([token_ok(), httpx.Response(200, json={"historyId": "9", "expiration": "1"}),
                     httpx.Response(200, json={"id": "s1", "threadId": "t1"})])
    c.watch("projects/p/topics/t")
    c.send("RAW", "t1")
    assert json.loads(rec.requests[1].content) == {"topicName": "projects/p/topics/t", "labelIds": ["INBOX"],
                                                   "labelFilterBehavior": "include"}
    assert json.loads(rec.requests[2].content) == {"raw": "RAW", "threadId": "t1"}


def test_build_raw_reply_headers():
    raw = build_raw(from_address="you@example.com", to=["a@x.com"], cc=["b@x.com"], subject="Re: Hi",
                    body="Body text", in_reply_to="<orig@x.com>", references="<first@x.com>")
    m = parse_raw(raw)
    assert m["To"] == "a@x.com" and m["Cc"] == "b@x.com" and m["Subject"] == "Re: Hi"
    assert m["In-Reply-To"] == "<orig@x.com>" and m["References"] == "<first@x.com> <orig@x.com>"
    assert m["X-Kings-AI"] == "outbound"
    assert m.get_content().strip() == "Body text"


# ---------- account auth lifecycle ----------
def test_client_factory_uses_fake_in_tests(db):
    assert isinstance(get_client(accounts.get_primary(db)), FakeGmailClient)


def test_real_client_requires_token(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "gmail_backend", "api")
    with pytest.raises(GmailAuthError, match="not authorised"):
        get_client(accounts.get_primary(db))


def test_store_token_activates_and_enqueues_sync(db, owner):
    out = store(db, "1//" + "x" * 40)
    account = accounts.get_primary(db)
    assert account.status == "active" and "Stored an encrypted token" in out
    assert crypto.decrypt(account.encrypted_refresh_token) == "1//" + "x" * 40
    types = sorted(j.type for j in db.execute(select(Job)).scalars())
    assert types == ["gmail_sync", "gmail_watch_renew"]
    with pytest.raises(SystemExit):
        store(db, "  ")


def test_auth_failure_alerts_once_and_reauth_rearms(db, owner, sent):
    owner.last_inbound_at = __import__("app.services.common", fromlist=["utcnow"]).utcnow()
    db.commit()
    account = accounts.get_primary(db)
    store_refresh_token(db, account, "1//" + "y" * 40)
    handle_auth_failure(db, account, GmailAuthError("invalid_grant"))
    handle_auth_failure(db, account, GmailAuthError("invalid_grant"))
    db.commit()
    assert account.status == "auth_error"
    run_jobs(["send_owner_message", "send_messages"])
    texts = [s["text"] for s in sent]
    assert len(texts) == 1 and "Gmail access" in texts[0] and "no email is lost" in texts[0]
    store_refresh_token(db, account, "1//" + "z" * 40)
    handle_auth_failure(db, account, GmailAuthError("revoked"))
    db.commit()
    run_jobs(["send_owner_message", "send_messages"])
    assert len(sent) == 2


# ---------- authorize script helpers ----------
def test_env_editing_preserves_other_lines():
    text = "# comment\nA=1\nGOOGLE_OAUTH_CLIENT_ID=old\nB=2\n"
    out = update_env_text(text, {"GOOGLE_OAUTH_CLIENT_ID": "new", "GOOGLE_OAUTH_CLIENT_SECRET": "s"})
    assert out == "# comment\nA=1\nGOOGLE_OAUTH_CLIENT_ID=new\nB=2\nGOOGLE_OAUTH_CLIENT_SECRET=s\n"


def test_pkce_pair():
    import base64
    import hashlib

    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected


def test_remote_env_editor_generates_key_once(tmp_path):
    import subprocess
    import sys

    from scripts.gmail_authorize import REMOTE_ENV_EDITOR

    env = tmp_path / ".env"
    env.write_text("A=1\n")
    payload = json.dumps({"GOOGLE_OAUTH_CLIENT_ID": "cid", "TOKEN_ENCRYPTION_KEY": "__generate_if_missing__"})
    for _ in range(2):
        subprocess.run([sys.executable, "-c", REMOTE_ENV_EDITOR, str(env)], input=payload, text=True, check=True,
                       capture_output=True)
    lines = env.read_text().splitlines()
    keys = [l.split("=", 1)[0] for l in lines]
    assert keys == ["A", "GOOGLE_OAUTH_CLIENT_ID", "TOKEN_ENCRYPTION_KEY"]
    key = lines[2].split("=", 1)[1]
    crypto_ok = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet(key.encode())
    assert crypto_ok and oct(env.stat().st_mode)[-3:] == "600"


def test_throttle_spaces_calls(monkeypatch):
    from app.email import gmail

    slept = []
    monkeypatch.setattr(gmail.time, "sleep", slept.append)
    t = gmail._Throttle(per_second=5)
    for _ in range(3):
        t.wait()
    assert len(slept) == 2 and all(0.15 < s <= 0.4 for s in slept)
