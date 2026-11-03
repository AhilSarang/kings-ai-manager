"""P3 M11: reports, detection rules, alerts, the daily summary and email linking."""

import gzip
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.amazon import inventory as inv
from app.amazon import listings as listings_svc
from app.amazon import monitor, reports
from app.amazon.fake import FakeAmazonClient as F
from app.db.models import AmazonAlert, AmazonListing, AmazonSalesDaily
from app.llm.fake import FakeLLMProvider
from tests.amazon.helpers import US, connect, expect_inventory, expect_listings, inventory_summary, issue, listing_item, sales
from tests.conftest import run_jobs
from tests.helpers import post, text_payload


@pytest.fixture
def client(db, owner):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def account(db, owner):
    from app.services.common import utcnow

    owner.last_inbound_at = utcnow()
    db.commit()
    return connect(db)


@pytest.fixture
def synced(db, account):
    expect_listings([listing_item(), listing_item(sku="VBZ-010", asin="B0VIBEZY10", title="Vibezy Lash Serum")])
    listings_svc.sync_all(db)
    return db.execute(select(AmazonListing).where(AmazonListing.sku == "SKZ-001")).scalar_one()


@pytest.fixture
def no_downloads(monkeypatch):
    """Report downloads come from the queued fixture instead of the network."""
    store = {}

    def fake_download(client, document_id):
        return store[document_id]

    monkeypatch.setattr(reports, "download", fake_download)
    monkeypatch.setattr(reports, "_sleep", lambda s: None)
    return store


def say(client, sent, text, *llm):
    FakeLLMProvider.script(*llm)
    before = len(sent)
    assert post(client, text_payload(text)).status_code == 200
    run_jobs(limit=100)
    return "\n---\n".join(s.get("text", f"[{s['kind']}]") for s in sent[before:])


def queue_report(document_id="DOC1"):
    F.expect(r"/reports/2021-06-30/reports$", {"reportId": "R1"})
    F.expect(r"/reports/2021-06-30/reports/R1", {"processingStatus": "DONE", "reportDocumentId": document_id})


# ---------- reports plumbing ----------
def test_tsv_reports_are_parsed(db, account, no_downloads):
    queue_report()
    no_downloads["DOC1"] = b"sku\treason\tstatus\nSKZ-001\tMissing images\tSuppressed\n"
    rows = reports.fetch(reports.LISTINGS_DEFECTS)
    assert rows == [{"sku": "SKZ-001", "reason": "Missing images", "status": "Suppressed"}]


def test_gzipped_reports_are_decompressed(db, account, monkeypatch):
    monkeypatch.setattr(reports, "_sleep", lambda s: None)
    payload = gzip.compress(b"sku\treason\nSKZ-001\tBoom\n")
    import httpx

    class FakeResponse:
        status_code = 200
        content = payload

    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse())
    queue_report()
    F.expect(r"/reports/2021-06-30/documents/DOC1",
             {"url": "https://x.test/doc", "compressionAlgorithm": "GZIP"})
    assert reports.fetch(reports.LISTINGS_DEFECTS)[0]["reason"] == "Boom"


def test_a_failed_report_says_so(db, account, monkeypatch):
    from app.amazon.errors import AmazonError

    monkeypatch.setattr(reports, "_sleep", lambda s: None)
    F.expect(r"/reports/2021-06-30/reports$", {"reportId": "R1"})
    F.expect(r"/reports/2021-06-30/reports/R1", {"processingStatus": "FATAL"})
    with pytest.raises(AmazonError, match="couldn't produce that report"):
        reports.fetch(reports.LISTINGS_DEFECTS)


# ---------- collectors ----------
def test_sales_collection_stores_units_and_traffic(db, synced, no_downloads):
    from app.services.common import utcnow

    yesterday = utcnow().date() - __import__("datetime").timedelta(days=1)
    queue_report()
    no_downloads["DOC1"] = json.dumps({"salesAndTrafficByAsin": [
        {"childAsin": "B0SKYZZ001", "date": str(yesterday),
         "salesByAsin": {"unitsOrdered": 14, "orderedProductSales": {"amount": 349.86, "currencyCode": "USD"}},
         "trafficByAsin": {"sessions": 210, "pageViews": 260, "buyBoxPercentage": 98.2}}]}).encode()
    assert monitor.collect_sales(db) == 1
    row = db.execute(select(AmazonSalesDaily)).scalar_one()
    assert row.units == 14 and float(row.ordered_product_sales) == 349.86 and row.sku == "SKZ-001"
    assert row.sessions == 210 and float(row.buy_box_pct) == 98.2


def test_health_reports_raise_alerts(db, synced, no_downloads):
    F.expect(r"/reports/2021-06-30/reports$", {"reportId": "R1"})
    F.expect(r"/reports/2021-06-30/reports/R1", {"processingStatus": "DONE", "reportDocumentId": "D1"})
    F.expect(r"/reports/2021-06-30/reports$", {"reportId": "R2"})
    F.expect(r"/reports/2021-06-30/reports/R2", {"processingStatus": "DONE", "reportDocumentId": "D2"})
    no_downloads["D1"] = b"sku\treason\nSKZ-001\tMain image missing\n"
    no_downloads["D2"] = b"sku\tstranded-reason\tquantity\nVBZ-010\tNo active listing\t23\n"
    counts = monitor.collect_health(db)
    assert counts == {"suppressed": 1, "stranded": 1}
    types = {a.type: a for a in db.execute(select(AmazonAlert)).scalars()}
    assert "Main image missing" in types["listing_suppressed"].details["message"]
    assert types["stranded"].details["units"] == "23"


# ---------- detection ----------
def test_listing_errors_alert_instantly_and_clear_when_fixed(db, sent, synced):
    synced.issues_cached = [issue(message="Main image is missing")]
    db.commit()
    assert monitor.detect(db)["listing_issue"] == 1
    db.commit()
    run_jobs(["send_owner_message", "send_messages"])
    alert = db.execute(select(AmazonAlert).where(AmazonAlert.type == "listing_issue")).scalar_one()
    assert alert.severity == "high" and alert.alerted_at is not None
    text = "\n".join(s.get("text", "") for s in sent)
    assert "AMAZON ALERT — Listing problem" in text and "Main image is missing" in text

    assert monitor.detect(db)["listing_issue"] == 0, "no repeat while it's still open"
    synced.issues_cached = []
    db.commit()
    monitor.detect(db)
    db.expire_all()
    assert db.execute(select(AmazonAlert).where(AmazonAlert.type == "listing_issue")).scalar_one().status \
        == "resolved"


def test_not_buyable_alerts(db, sent, synced):
    synced.buyable = False
    synced.status_cached = "INACTIVE"
    db.commit()
    assert monitor.detect(db)["not_buyable"] >= 1
    alert = db.execute(select(AmazonAlert).where(AmazonAlert.type == "listing_suppressed")).scalars().first()
    assert "no longer buyable" in alert.details["message"]


def test_out_of_stock_but_findable_is_not_called_suppressed(db, sent, synced):
    """The live account had 84 listings like this; every one of them used to be an alert."""
    expect_inventory([inventory_summary(fulfillable=0)])
    inv.snapshot(db)
    synced.buyable = False
    synced.status_cached = "DISCOVERABLE"      # Amazon drops BUYABLE when there's nothing to sell
    db.commit()
    assert monitor.detect(db)["not_buyable"] == 0
    assert db.execute(select(AmazonAlert).where(AmazonAlert.type == "listing_suppressed")).first() is None


def test_stock_on_hand_that_nobody_can_buy_is_suppressed(db, sent, synced):
    expect_inventory([inventory_summary(fulfillable=140)])
    inv.snapshot(db)
    synced.buyable = False
    synced.status_cached = "DISCOVERABLE"
    db.commit()
    assert monitor.detect(db)["not_buyable"] == 1
    alert = db.execute(select(AmazonAlert).where(AmazonAlert.type == "listing_suppressed")).scalar_one()
    assert "no longer buyable" in alert.details["message"]


def test_a_pile_of_problems_arrives_as_one_message(db, sent, account):
    expect_listings([listing_item(sku=f"SKZ-{n:03d}", asin=f"B0SKYZZ{n:03d}",
                                  issues=[issue(message="Main image is missing")]) for n in range(1, 8)])
    listings_svc.sync_all(db)
    counts = monitor.detect(db)
    db.commit()
    run_jobs(["send_owner_message", "send_messages"])
    assert counts["listing_issue"] == 7
    texts = [s.get("text", "") for s in sent]
    assert len(texts) == 1, texts
    assert "7 Amazon problems need you" in texts[0] and "Listing problem (7)" in texts[0]
    assert "and 2 more" in texts[0] and "Reply ISSUES for the full list" in texts[0]
    assert all(a.alerted_at is not None for a in db.execute(select(AmazonAlert)).scalars())


def test_stockout_with_recent_sales_is_urgent(db, sent, synced):
    expect_inventory([inventory_summary(fulfillable=0, inbound_shipped=0)])
    inv.snapshot(db)
    sales(db, "SKZ-001", units_per_day=3, days=14)
    assert monitor.detect(db)["stockout"] == 1
    db.commit()
    run_jobs(["send_owner_message", "send_messages"])
    text = "\n".join(s.get("text", "") for s in sent)
    assert "is out of stock at Amazon and sold" in text and "Nothing is inbound" in text
    assert "draft an FBA shipment" in text


def test_stockout_without_sales_is_not_an_alert(db, synced):
    expect_inventory([inventory_summary(fulfillable=0)])
    inv.snapshot(db)
    assert monitor.detect(db)["stockout"] == 0


def test_low_stock_is_a_summary_item_until_it_gets_urgent(db, sent, synced):
    expect_inventory([inventory_summary(fulfillable=60)])
    inv.snapshot(db)
    sales(db, "SKZ-001", units_per_day=4)  # 15 days of cover
    assert monitor.detect(db)["low_stock"] == 1
    alert = db.execute(select(AmazonAlert).where(AmazonAlert.type == "low_stock")).scalar_one()
    assert alert.severity == "medium" and alert.alerted_at is None, "quiet: it belongs in the daily summary"

    db.delete(alert)
    db.commit()
    expect_inventory([inventory_summary(fulfillable=12)])
    inv.snapshot(db)
    monitor.detect(db)
    db.commit()
    run_jobs(["send_owner_message", "send_messages"])
    urgent = db.execute(select(AmazonAlert).where(AmazonAlert.type == "low_stock")).scalar_one()
    assert urgent.severity == "high" and urgent.alerted_at is not None
    assert any("days of cover" in s.get("text", "") for s in sent)


def test_sales_drop_needs_a_real_baseline(db, synced):
    from datetime import timedelta

    from app.services.common import utcnow

    today = utcnow().date()
    for i in range(8, 15):  # last week: 70 units
        db.add(AmazonSalesDaily(marketplace_id=US, asin="B0SKYZZ001", sku="SKZ-001",
                                date=today - timedelta(days=i), units=10))
    for i in range(1, 8):   # this week: 21 units, a 70% drop
        db.add(AmazonSalesDaily(marketplace_id=US, asin="B0SKYZZ001", sku="SKZ-001",
                                date=today - timedelta(days=i), units=3))
    db.commit()
    assert monitor.detect(db)["sales_drop"] == 1
    alert = db.execute(select(AmazonAlert).where(AmazonAlert.type == "sales_drop")).scalar_one()
    assert alert.alerted_at is None, "sales drops go in the daily summary, not an interruption"


def test_tiny_numbers_do_not_trigger_a_sales_drop(db, synced):
    from datetime import timedelta

    from app.services.common import utcnow

    today = utcnow().date()
    for i in range(8, 15):
        db.add(AmazonSalesDaily(marketplace_id=US, asin="B0SKYZZ001", sku="SKZ-001",
                                date=today - timedelta(days=i), units=1))
    db.commit()
    assert monitor.detect(db)["sales_drop"] == 0


def test_price_changed_outside_the_system(db, synced):
    monitor.price_changed_outside(db, synced, 24.99, 19.99)
    alert = db.execute(select(AmazonAlert).where(AmazonAlert.type == "price_anomaly")).scalar_one()
    assert "24.99 to 19.99" in alert.details["message"] and "without going through me" in alert.details["message"]


# ---------- commands ----------
def test_issues_command(client, db, sent, synced):
    synced.issues_cached = [issue(message="Main image is missing")]
    other = db.execute(select(AmazonListing).where(AmazonListing.sku == "VBZ-010")).scalar_one()
    other.issues_cached = [issue(severity="WARNING", message="Add more bullet points")]
    db.commit()
    out = say(client, sent, "ISSUES")
    assert "❗ L-1 SKZ-001" in out and "Main image is missing" in out
    assert "⚠️ L-2 VBZ-010" in out and "Add more bullet points" in out
    assert FakeLLMProvider.requests == []


def test_issues_command_when_everything_is_fine(client, db, sent, synced):
    assert "No listing errors or warnings" in say(client, sent, "ISSUES")


def test_sales_command(client, db, sent, synced):
    from datetime import timedelta

    from app.services.common import utcnow

    yesterday = utcnow().date() - timedelta(days=1)
    db.add(AmazonSalesDaily(marketplace_id=US, asin="B0SKYZZ001", sku="SKZ-001", date=yesterday, units=14,
                            ordered_product_sales=349.86, currency="USD"))
    db.add(AmazonSalesDaily(marketplace_id=US, asin="B0VIBEZY10", sku="VBZ-010", date=yesterday, units=3,
                            ordered_product_sales=55.5, currency="USD"))
    db.commit()
    out = say(client, sent, "SALES")
    assert "Yesterday" in out and "17 units, USD 405.36" in out
    assert "SKZ-001: 14 units" in out


def test_sales_command_before_any_data(client, db, sent, synced):
    assert "No sales data yet" in say(client, sent, "sales")


# ---------- daily summary ----------
def test_daily_summary_gets_amazon_sections(db, sent, synced, owner):
    from app.services.summaries import daily_summary

    monitor.raise_alert(db, type_="low_stock", sku="SKZ-001",
                        details={"message": "SKZ-001 has about 9 days of cover (36 units)."})
    monitor.raise_alert(db, type_="stranded", sku="VBZ-010",
                        details={"message": "VBZ-010 has stranded inventory: no active listing"})
    db.commit()
    text = daily_summary(db)
    assert "*Stock to watch*" in text and "9 days of cover" in text
    assert "*Stranded inventory*" in text


# ---------- email linking ----------
def test_amazon_emails_are_linked_to_the_listing_they_mention(db, synced, account):
    from app.email.sync import ingest_message
    from tests.email.helpers import GENUINE_AMAZON_AUTH, connect as connect_email, receive

    email_account = connect_email(db)
    message = receive(db, email_account, from_="seller-performance@amazon.com",
                      subject="Action required for SKZ-001",
                      body="Your listing SKZ-001 has a policy issue.",
                      headers={"Authentication-Results": GENUINE_AMAZON_AUTH})
    message.thread.category = "amazon"
    monitor.raise_alert(db, type_="listing_issue", sku="SKZ-001",
                        details={"message": "SKZ-001 has a problem"})
    db.commit()
    assert monitor.link_amazon_emails(db) == 1
    alert = db.execute(select(AmazonAlert).where(AmazonAlert.type == "listing_issue")).scalar_one()
    assert alert.linked_email_thread_id == message.thread_id


def test_unrelated_amazon_email_is_not_linked(db, synced, account):
    from tests.email.helpers import connect as connect_email, receive

    email_account = connect_email(db)
    message = receive(db, email_account, from_="marketing@amazon.com", subject="Try Amazon Ads",
                      body="Grow your sales with ads.")
    message.thread.category = "amazon"
    monitor.raise_alert(db, type_="listing_issue", sku="SKZ-001", details={"message": "x"})
    db.commit()
    assert monitor.link_amazon_emails(db) == 0
