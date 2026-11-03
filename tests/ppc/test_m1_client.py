"""P4 M1: Ads API client — tokens, profile scoping, headers, retries, async reports, the fake."""

import gzip
import json

import httpx
import pytest

from app.ppc import client as client_mod
from app.ppc import regions
from app.ppc.client import HttpAdsClient
from app.ppc.errors import (AdsAuthError, AdsError, AdsReportFailed, AdsReportInFlight,
                            AdsRetryable, problem_text)
from app.ppc.fake import FakeAdsClient
from app.ppc.factory import get_client


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        response = self.responses.pop(0)
        return response(request) if callable(response) else response


def token_ok(expires=3600):
    return httpx.Response(200, json={"access_token": "Atza|AT", "expires_in": expires})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr("app.ppc.client._sleep", slept.append)
    monkeypatch.setattr(client_mod.API_THROTTLE, "interval", 0)   # spacing is tested on its own
    return slept


def client(responses, **kw):
    t = Transport(responses)
    c = HttpAdsClient(client_id="cid", client_secret="sec", refresh_token="Atzr|RT", region="na",
                      account_id=kw.pop("account_id", "ACC-1"),
                      http=httpx.Client(transport=httpx.MockTransport(t)), **kw)
    return c, t


# ---------- regions ----------
def test_regional_hosts():
    assert regions.endpoint("na") == "https://advertising-api.amazon.com"
    assert regions.endpoint("EU") == "https://advertising-api-eu.amazon.com"
    assert regions.region_for("ca") == "na" and regions.region_for("JP") == "fe"
    with pytest.raises(KeyError, match="Unknown Ads region 'us'"):
        regions.endpoint("us")


# ---------- auth ----------
def test_token_fetched_once_and_reused():
    c, t = client([token_ok(), httpx.Response(200, json={"ok": 1}), httpx.Response(200, json={"ok": 2})])
    assert c.get("/sp/campaigns/list", profile_id="P1") == {"ok": 1}
    c.get("/sp/campaigns/list", profile_id="P1")
    assert len(t.requests) == 3                      # one token call, two API calls
    assert str(t.requests[0].url).endswith("/auth/o2/token")


def test_every_call_carries_the_headers_amazon_requires():
    c, t = client([token_ok(), httpx.Response(200, json={})])
    c.get("/sp/campaigns/list", profile_id="P1")
    headers = t.requests[1].headers
    assert headers["authorization"] == "Bearer Atza|AT"
    assert headers["amazon-advertising-api-clientid"] == "cid"
    assert headers["amazon-ads-clientid"] == "cid"          # reporting v3 spells it this way
    assert headers["amazon-advertising-api-scope"] == "P1"
    assert "amazon-ads-accountid" not in headers, "only reporting needs the account id"


def test_reporting_carries_the_account_id():
    c, t = client([token_ok(), httpx.Response(200, json={"reportId": "R1"})])
    c.post("/reporting/reports", json={"name": "x"}, profile_id="P1",
           content_type=client_mod.REPORT_CONTENT_TYPE)
    headers = t.requests[1].headers
    assert headers["amazon-ads-accountid"] == "ACC-1"
    assert headers["content-type"] == "application/vnd.createasyncreportrequest.v3+json"


def test_missing_credentials_is_an_auth_error():
    c = HttpAdsClient(client_id="", client_secret="", refresh_token="", http=httpx.Client())
    with pytest.raises(AdsAuthError, match="not configured"):
        c.get("/anything", profile_id="P1")


def test_a_refused_refresh_token_says_what_to_do():
    c, _ = client([httpx.Response(400, json={"error": "invalid_grant"})])
    with pytest.raises(AdsAuthError, match="365 days"):
        c.get("/sp/campaigns/list", profile_id="P1")


# ---------- errors and retries ----------
def test_429_backs_off_on_retry_after(no_sleep):
    c, t = client([token_ok(), httpx.Response(429, headers={"retry-after": "5"}),
                   httpx.Response(200, json={"ok": True})])
    assert c.get("/sp/campaigns/list", profile_id="P1") == {"ok": True}
    assert no_sleep == [5.0] and len(t.requests) == 3


def test_5xx_backs_off_exponentially_then_gives_up(no_sleep):
    c, t = client([token_ok()] + [httpx.Response(502)] * 4)
    with pytest.raises(AdsRetryable):
        c.get("/sp/campaigns/list", profile_id="P1")
    assert no_sleep == [1.0, 2.0, 4.0] and len(t.requests) == 5


def test_403_names_the_profile_problem():
    c, _ = client([token_ok(), httpx.Response(403, json={"details": "Not authorized"})])
    with pytest.raises(AdsAuthError, match="isn't authorised for that profile") as e:
        c.get("/sp/campaigns/list", profile_id="P1")
    assert e.value.missing_scope is True, "one profile being off-limits is not a broken connection"


def test_400_is_permanent_and_readable():
    body = {"errors": [{"field": "budget", "message": "must be greater than 1.00"}]}
    c, t = client([token_ok(), httpx.Response(400, json=body)])
    with pytest.raises(AdsError, match="must be greater than 1.00") as e:
        c.put("/sp/campaigns", json={}, profile_id="P1")
    assert "budget" in str(e.value) and len(t.requests) == 2      # no retry


def test_problem_text_handles_the_shapes_amazon_uses():
    assert "over limit" in problem_text({"details": "over limit"})
    assert "bad bid" in problem_text([{"message": "bad bid", "field": "bid"}])
    assert problem_text({"errors": [{"message": "nope"}]}) == "nope"
    assert problem_text(None) == ""


# ---------- asynchronous reports ----------
def test_a_report_is_requested_polled_downloaded_and_parsed(no_sleep):
    rows = [{"date": "2026-09-01", "campaignId": "C1", "clicks": 12, "cost": 4.5}]
    payload = gzip.compress(json.dumps(rows).encode())
    c, t = client([token_ok(),
                   httpx.Response(200, json={"reportId": "R1", "status": "PENDING"}),
                   httpx.Response(200, json={"reportId": "R1", "status": "PENDING"}),
                   httpx.Response(200, json={"reportId": "R1", "status": "COMPLETED",
                                             "url": "https://reports.test/R1.json.gz"}),
                   httpx.Response(200, content=payload)])
    got = c.fetch_report({"name": "SP campaigns"}, profile_id="P1")
    assert got == rows
    assert no_sleep == [client_mod.REPORT_POLL_SECONDS], "waited between polls rather than hammering"


def test_a_failed_report_says_why(no_sleep):
    c, _ = client([token_ok(),
                   httpx.Response(200, json={"reportId": "R1"}),
                   httpx.Response(200, json={"status": "FAILURE", "failureReason": "date range too long"})])
    with pytest.raises(AdsReportFailed, match="date range too long"):
        c.fetch_report({"name": "x"}, profile_id="P1")


def test_a_report_that_never_finishes_times_out_without_loading_anything(no_sleep):
    c, _ = client([token_ok()] + [httpx.Response(200, json={"reportId": "R1", "status": "PENDING"})] * 40)
    with pytest.raises(AdsReportFailed, match="nothing was loaded") as e:
        c.wait_for_report("R1", profile_id="P1", timeout=0, poll=1)
    assert e.value.timed_out is True


def test_a_report_with_no_download_link_is_an_error(no_sleep):
    c, _ = client([token_ok()])
    with pytest.raises(AdsError, match="no download link"):
        c.report_rows({"status": "COMPLETED"})


# ---------- throttle ----------
def test_calls_are_spaced(monkeypatch):
    slept = []
    monkeypatch.setattr(client_mod, "_sleep", slept.append)
    throttle = client_mod._Throttle(per_second=2)
    for _ in range(3):
        throttle.wait()          # the first goes straight through; the next two are spaced
    assert len(slept) == 2 and slept[0] == pytest.approx(0.5, abs=0.05)
    assert client_mod.REQUESTS_PER_SECOND <= 2.0, "Ads rate limits are tighter than SP-API's"


# ---------- the fake ----------
def test_the_fake_is_used_in_tests():
    assert isinstance(get_client(), FakeAdsClient), "no test may reach the live Ads API"


def test_the_fake_matches_queued_responses_and_records_calls():
    FakeAdsClient.expect(r"/sp/campaigns/list", {"campaigns": [{"campaignId": "C1"}]})
    c = FakeAdsClient()
    assert c.post("/sp/campaigns/list", json={"stateFilter": "ENABLED"},
                  profile_id="P1")["campaigns"][0]["campaignId"] == "C1"
    [call] = FakeAdsClient.calls_to("campaigns")
    assert call["profile_id"] == "P1" and call["json"] == {"stateFilter": "ENABLED"}
    assert FakeAdsClient.last_body("campaigns") == {"stateFilter": "ENABLED"}


def test_the_fake_runs_the_whole_report_dance():
    rows = [{"date": "2026-09-01", "clicks": 3}]
    FakeAdsClient.expect_report(rows)
    assert FakeAdsClient().fetch_report({"name": "x"}, profile_id="P1") == rows


def test_the_fake_complains_about_an_unqueued_call():
    with pytest.raises(AdsError, match="no queued response"):
        FakeAdsClient().get("/sp/campaigns/list", profile_id="P1")


# ---------- corrections from M3's reading of the live docs ----------
def test_a_duplicate_report_request_is_not_treated_as_a_rate_limit(no_sleep):
    """425 means one is already generating. Retrying the create immediately just earns another."""
    c, t = client([token_ok(), httpx.Response(425, json={"details": "duplicate of a processing request"}),
                   httpx.Response(200, json={"reportId": "R1"})])
    report_id = c.request_report({"name": "x"}, profile_id="P1")
    assert report_id == "R1"
    assert no_sleep == [client_mod.IN_FLIGHT_WAIT_SECONDS], "it waited for the one already running"
    assert len(t.requests) == 3


def test_a_report_that_stays_duplicated_gives_up_saying_so(no_sleep):
    c, _ = client([token_ok()] + [httpx.Response(425, json={})] * client_mod.IN_FLIGHT_ATTEMPTS)
    with pytest.raises(AdsReportInFlight):
        c.request_report({"name": "x"}, profile_id="P1")


def test_polling_slows_down_rather_than_hammering(no_sleep):
    c, _ = client([token_ok()] + [httpx.Response(200, json={"status": "PENDING"})] * 3 +
                  [httpx.Response(200, json={"status": "COMPLETED", "url": "u"})])
    c.wait_for_report("R1", profile_id="P1", timeout=1000, poll=30)
    assert no_sleep == [30, 45.0, 67.5], "status calls are themselves rate limited"


def test_the_timeout_allows_for_the_three_hours_amazon_admits_to():
    assert client_mod.REPORT_TIMEOUT_SECONDS >= 3 * 3600, \
        "Amazon's FAQ says generation can take three hours; giving up sooner loses a day of data"


def test_an_expired_download_link_is_asked_for_again(no_sleep):
    rows = [{"date": "2026-09-01", "clicks": 1}]
    payload = gzip.compress(json.dumps(rows).encode())
    c, t = client([token_ok(),
                   httpx.Response(200, json={"reportId": "R1", "status": "COMPLETED",
                                             "url": "https://old.test/r.gz",
                                             "urlExpiresAt": "2020-01-01T00:00:00Z"}),
                   httpx.Response(200, content=payload)])
    got = c.report_rows({"reportId": "R1", "status": "COMPLETED", "url": "https://old.test/r.gz",
                         "urlExpiresAt": "2020-01-01T00:00:00Z"}, profile_id="P1")
    assert got == rows
    assert "/reporting/reports/R1" in str(t.requests[1].url), "it re-read the report for a fresh link"


def test_a_link_that_has_not_expired_is_used_as_it_is(no_sleep):
    rows = [{"clicks": 2}]
    # A presigned download carries no Ads headers, so it needs no token either.
    c, t = client([httpx.Response(200, content=gzip.compress(json.dumps(rows).encode()))])
    assert c.report_rows({"reportId": "R1", "url": "https://fresh.test/r.gz",
                          "urlExpiresAt": "2099-01-01T00:00:00Z"}, profile_id="P1") == rows
    assert len(t.requests) == 1, "no needless second call for the report record"
