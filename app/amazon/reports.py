"""Amazon reports: request, wait, download, parse (brief 13.1).

Reports are asynchronous and come back as a gzipped document, so this hides all of that behind
`fetch(...)` and hands back rows.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import time
from typing import Any

import httpx

from app.amazon import api
from app.amazon import marketplaces as mkt
from app.amazon.errors import AmazonError, AmazonRetryable

log = logging.getLogger(__name__)

POLL_SECONDS = 5
TIMEOUT_SECONDS = 600
DONE = {"DONE", "FATAL", "CANCELLED"}

LISTINGS_DEFECTS = "GET_MERCHANT_LISTINGS_DEFECT_DATA"
STRANDED = "GET_STRANDED_INVENTORY_UI_DATA"
SALES_AND_TRAFFIC = "GET_SALES_AND_TRAFFIC_REPORT"


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def request_report(client, report_type: str, marketplace_id: str, *, options: dict[str, Any] | None = None,
                   start: str | None = None, end: str | None = None) -> str:
    body: dict[str, Any] = {"reportType": report_type, "marketplaceIds": [marketplace_id]}
    if options:
        body["reportOptions"] = options
    if start:
        body["dataStartTime"] = start
    if end:
        body["dataEndTime"] = end
    response = client.post("/reports/2021-06-30/reports", json=body, operation="createReport")
    report_id = response.get("reportId")
    if not report_id:
        raise AmazonError(f"Amazon didn't accept the {report_type} report request.")
    return report_id


def wait_for(client, report_id: str, *, timeout: float = TIMEOUT_SECONDS) -> str:
    """Returns the document id once the report is DONE."""
    deadline = time.monotonic() + timeout
    while True:
        report = client.get(f"/reports/2021-06-30/reports/{report_id}", operation="getReport")
        status = (report.get("processingStatus") or "").upper()
        if status == "DONE":
            return report.get("reportDocumentId")
        if status in ("FATAL", "CANCELLED"):
            raise AmazonError(f"Amazon couldn't produce that report ({status.lower()}).")
        if time.monotonic() >= deadline:
            raise AmazonRetryable(f"Report {report_id} is still processing after {int(timeout)}s.")
        _sleep(POLL_SECONDS)


def download(client, document_id: str) -> bytes:
    document = client.get(f"/reports/2021-06-30/documents/{document_id}", operation="getReportDocument")
    url = document.get("url")
    if not url:
        raise AmazonError("Amazon returned a report with no download link.")
    response = httpx.get(url, timeout=120, follow_redirects=True)
    if response.status_code != 200:
        raise AmazonRetryable(f"Could not download the report ({response.status_code}).")
    data = response.content
    if (document.get("compressionAlgorithm") or "").upper() == "GZIP" or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def parse_rows(data: bytes) -> list[dict[str, str]]:
    """Amazon's flat files are tab separated with a header row."""
    text = data.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def fetch(report_type: str, marketplace_code: str = "US", *, options: dict[str, Any] | None = None,
          start: str | None = None, end: str | None = None) -> list[dict[str, str]]:
    client = api.client()
    mp = mkt.by_code(marketplace_code)
    report_id = request_report(client, report_type, mp.marketplace_id, options=options, start=start, end=end)
    document_id = wait_for(client, report_id)
    return parse_rows(download(client, document_id))


def fetch_json(report_type: str, marketplace_code: str = "US", *, options: dict[str, Any] | None = None,
               start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Sales & Traffic comes back as JSON rather than a flat file."""
    client = api.client()
    mp = mkt.by_code(marketplace_code)
    report_id = request_report(client, report_type, mp.marketplace_id, options=options, start=start, end=end)
    document_id = wait_for(client, report_id)
    raw = download(client, document_id)
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise AmazonError("Amazon's sales report wasn't readable JSON.") from exc
