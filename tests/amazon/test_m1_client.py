"""P3 M1: SP-API client — tokens, retries, rate limits, async operations, and the fake backend."""

import httpx
import pytest

from app.amazon import marketplaces as mkt
from app.amazon import client as client_mod
from app.amazon.client import HttpAmazonClient
from app.amazon.errors import AmazonAuthError, AmazonError, AmazonOperationFailed, AmazonRetryable, problem_text
from app.amazon.factory import get_client
from app.amazon.fake import FakeAmazonClient


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        resp = self.responses.pop(0)
        return resp(request) if callable(resp) else resp


def token_ok(expires=3600):
    return httpx.Response(200, json={"access_token": "AT", "expires_in": expires})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr("app.amazon.client._sleep", slept.append)
    monkeypatch.setattr(client_mod.API_THROTTLE, "interval", 0)  # spacing is tested in M13
    return slept


def client(responses, **kw):
    t = Transport(responses)
    c = HttpAmazonClient(client_id="cid", client_secret="sec", refresh_token="Atzr|RT", region="na",
                         http=httpx.Client(transport=httpx.MockTransport(t)), **kw)
    return c, t


# ---------- marketplaces ----------
def test_marketplace_config():
    us = mkt.by_code("us")
    assert (us.marketplace_id, us.currency, us.locale, us.fulfillment_channel) == \
        ("ATVPDKIKX0DER", "USD", "en_US", "AMAZON_NA")
    assert mkt.by_id("A2EUQ1WTGCTBG2").code == "CA"
    assert [m.code for m in mkt.enabled("US, CA")] == ["US", "CA"]
    with pytest.raises(KeyError, match="Unknown marketplace 'UK'"):
        mkt.by_code("UK")


# ---------- auth ----------
def test_token_fetched_once_and_reused():
    c, t = client([token_ok(), httpx.Response(200, json={"payload": 1}), httpx.Response(200, json={"payload": 2})])
    assert c.get("/sellers/v1/marketplaceParticipations") == {"payload": 1}
    c.get("/sellers/v1/marketplaceParticipations")
    assert [str(r.url).split("amazon.com")[-1] for r in t.requests][0] == "/auth/o2/token"
    assert len(t.requests) == 3  # token + two calls
    assert t.requests[1].headers["x-amz-access-token"] == "AT"


def test_refresh_token_rejected_is_auth_error():
    c, _ = client([httpx.Response(400, json={"error": "invalid_grant"})])
    with pytest.raises(AmazonAuthError, match="invalid_grant"):
        c.get("/anything")


def test_missing_credentials_is_auth_error():
    c = HttpAmazonClient(client_id="", client_secret="", refresh_token="", http=httpx.Client())
    with pytest.raises(AmazonAuthError, match="not configured"):
        c.get("/anything")


def test_lwa_server_error_is_retryable():
    c, _ = client([httpx.Response(503)])
    with pytest.raises(AmazonRetryable):
        c.get("/anything")


# ---------- errors and retries ----------
def test_429_retries_then_succeeds(no_sleep):
    c, t = client([token_ok(),
                   httpx.Response(429, headers={"retry-after": "3"}),
                   httpx.Response(200, json={"ok": True})])
    assert c.get("/x") == {"ok": True}
    assert no_sleep == [3.0] and len(t.requests) == 3


def test_rate_limit_header_sets_the_wait(no_sleep):
    c, _ = client([token_ok(),
                   httpx.Response(429, headers={"x-amzn-RateLimit-Limit": "0.5"}),
                   httpx.Response(200, json={"ok": True})])
    c.get("/x")
    assert no_sleep == [2.0]  # 0.5 requests/second -> wait 2s


def test_5xx_backs_off_exponentially_then_gives_up(no_sleep):
    c, t = client([token_ok()] + [httpx.Response(500)] * 4)
    with pytest.raises(AmazonRetryable):
        c.get("/x")
    assert no_sleep == [1.0, 2.0, 4.0] and len(t.requests) == 5


def test_403_missing_role_explains_itself():
    body = {"errors": [{"code": "Unauthorized", "message": "Access to requested resource is denied."}]}
    c, _ = client([token_ok(), httpx.Response(403, json=body)])
    with pytest.raises(AmazonAuthError, match="missing a role"):
        c.get("/inbound/fba/2024-03-20/inboundPlans")


def test_400_is_permanent_and_readable():
    body = {"errors": [{"code": "InvalidInput", "message": "Box weight is required"}]}
    c, t = client([token_ok(), httpx.Response(400, json=body)])
    with pytest.raises(AmazonError, match="Box weight is required") as e:
        c.post("/inbound/fba/2024-03-20/inboundPlans", json={})
    assert "InvalidInput" in str(e.value) and len(t.requests) == 2  # no retry


def test_network_error_retries_then_raises(no_sleep):
    def boom(request):
        raise httpx.ConnectError("no route")

    c, _ = client([token_ok()] + [boom] * 4)
    with pytest.raises(AmazonRetryable, match="unreachable"):
        c.get("/x")


# ---------- call recording ----------
def test_calls_are_recorded_with_request_id():
    records = []
    c, _ = client([token_ok(),
                   httpx.Response(200, json={"ok": 1}, headers={"x-amzn-RequestId": "req-9"}),
                   httpx.Response(400, json={"errors": [{"code": "Bad", "message": "nope"}]})],
                  recorder=records.append)
    c.get("/listings/2021-08-01/items/SELLER/SKU-1", params={"marketplaceIds": "ATVPDKIKX0DER"},
          operation="getListingsItem")
    with pytest.raises(AmazonError):
        c.get("/x", operation="somethingElse")
    assert records[0]["operation"] == "getListingsItem" and records[0]["request_id"] == "req-9"
    assert records[0]["http_status"] == 200 and records[0]["marketplace_id"] == "ATVPDKIKX0DER"
    assert records[0]["duration_ms"] >= 0
    assert records[1]["http_status"] == 400 and "nope" in records[1]["error_code"]


def test_recorder_failure_never_breaks_a_call():
    def bad(record):
        raise RuntimeError("db down")

    c, _ = client([token_ok(), httpx.Response(200, json={"ok": 1})], recorder=bad)
    assert c.get("/x") == {"ok": 1}


# ---------- async operations ----------
def test_await_operation_polls_until_success(no_sleep):
    FakeAmazonClient.operation("op-1", [{"operationStatus": "IN_PROGRESS"},
                                        {"operationStatus": "IN_PROGRESS"},
                                        {"operationStatus": "SUCCESS"}])
    c = FakeAmazonClient()
    assert c.await_operation("op-1")["operationStatus"] == "SUCCESS"
    assert len(FakeAmazonClient.calls_to("/operations/op-1")) == 3
    assert no_sleep == [2.0, 2.0]


def test_await_operation_reports_problems_in_plain_english():
    FakeAmazonClient.operation("op-2", [{"operationStatus": "FAILED", "operationProblems": [
        {"code": "BOX_WEIGHT_EXCEEDED", "message": "Box 3 is over 50 lb", "severity": "ERROR"}]}])
    with pytest.raises(AmazonOperationFailed, match="Box 3 is over 50 lb"):
        FakeAmazonClient().await_operation("op-2")


def test_await_operation_times_out_without_confirming(no_sleep):
    FakeAmazonClient.default_operation_status = "IN_PROGRESS"
    with pytest.raises(AmazonOperationFailed, match="nothing was confirmed") as e:
        FakeAmazonClient().await_operation("op-3", timeout=0)
    assert e.value.timed_out


def test_http_operation_status_uses_the_inbound_endpoint():
    c, t = client([token_ok(), httpx.Response(200, json={"operationStatus": "SUCCESS"})])
    assert c.operation_status("op-9")["operationStatus"] == "SUCCESS"
    assert t.requests[1].url.path == "/inbound/fba/2024-03-20/operations/op-9"


# ---------- fake backend ----------
def test_factory_returns_the_fake_in_tests():
    assert isinstance(get_client(), FakeAmazonClient)


def test_fake_matches_queued_responses_and_records_calls():
    FakeAmazonClient.expect_many([(r"/catalog/2022-04-01/items", {"items": [{"asin": "B0TEST"}]}),
                                  (r"/listings/2021-08-01/items", {"sku": "SKU-1"})])
    c = get_client()
    assert c.get("/catalog/2022-04-01/items", params={"keywords": "tape"})["items"][0]["asin"] == "B0TEST"
    assert c.patch("/listings/2021-08-01/items/S/SKU-1", json={"patches": []})["sku"] == "SKU-1"
    assert [c_["method"] for c_ in FakeAmazonClient.calls] == ["GET", "PATCH"]
    assert FakeAmazonClient.last_body("/listings") == {"patches": []}


def test_fake_can_raise_and_complains_about_unqueued_calls():
    FakeAmazonClient.expect(r"/x", AmazonRetryable("rate limited"))
    with pytest.raises(AmazonRetryable):
        get_client().get("/x")
    with pytest.raises(AmazonError, match="no queued response"):
        get_client().get("/unexpected")


def test_problem_text_handles_amazon_shapes():
    assert problem_text([{"code": "X", "message": "boom"}]) == "boom [X]"
    assert problem_text({"message": "single"}) == "single"
    assert problem_text(None) == ""
    assert "one" in problem_text([{"message": "one"}, {"message": "two"}])
