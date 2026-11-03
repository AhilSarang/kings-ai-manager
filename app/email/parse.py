"""Turn a Gmail API message (format=full) into plain, safe fields.

- Prefers text/plain; converts HTML to text and drops hidden content
  (display:none, visibility:hidden, zero font size, white text), flagging it.
- Trusts only Gmail's own Authentication-Results header (authserv-id mx.google.com),
  so a sender can't forge "dkim=pass" by adding their own header.
- Never follows links or opens attachments.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from app.email.gmail import OUTBOUND_HEADER, OUTBOUND_VALUE, b64url_decode

KEEP_HEADERS = ("From", "To", "Cc", "Reply-To", "Subject", "Date", "Message-ID", "In-Reply-To", "References",
                "Authentication-Results", "List-Unsubscribe", "Precedence", "Auto-Submitted", OUTBOUND_HEADER)
CATEGORY_LABELS = {"CATEGORY_PERSONAL": "Primary", "CATEGORY_PROMOTIONS": "Promotions",
                   "CATEGORY_SOCIAL": "Social", "CATEGORY_UPDATES": "Updates", "CATEGORY_FORUMS": "Forums"}
TRUSTED_AUTHSERV = "mx.google.com"
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d]*[1-9])|"
    r"(?<![-\w])color\s*:\s*(#fff(?:fff)?\b|white\b|rgb\(\s*255\s*,\s*255\s*,\s*255\s*\))|opacity\s*:\s*0(?![.\d]*[1-9])",
    re.I,
)
_VOID = {"br", "img", "hr", "meta", "link", "input", "area", "base", "col", "embed", "source", "track", "wbr"}
_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "blockquote"}


@dataclass
class ParsedAttachment:
    filename: str
    mime_type: str
    size: int
    attachment_id: str | None


@dataclass
class ParsedEmail:
    provider_message_id: str
    provider_thread_id: str
    headers: dict[str, str]
    from_name: str
    from_address: str
    to: list[str]
    cc: list[str]
    reply_to: str | None
    subject: str
    sent_at: datetime | None
    rfc_message_id: str | None
    body_text: str
    snippet: str
    labels: list[str]
    gmail_category: str | None
    auth_spf: str = "none"
    auth_dkim: str = "none"
    auth_dmarc: str = "none"
    dkim_domains: list[str] = field(default_factory=list)
    attachments: list[ParsedAttachment] = field(default_factory=list)
    hidden_text_detected: bool = False
    is_self_sent: bool = False


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[bool] = []  # hidden flags for open elements
        self.hidden_depth = 0
        self.skip_depth = 0
        self.hidden_found = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "head", "title"):
            self.skip_depth += 1
            return
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        hidden = bool(_HIDDEN_STYLE.search(attrs_d.get("style", ""))) or "hidden" in attrs_d
        if tag in _VOID:
            if tag == "br" and not self.hidden_depth:
                self.parts.append("\n")
            return
        self.stack.append(hidden)
        if hidden:
            self.hidden_depth += 1
        if tag in _BLOCK and not self.hidden_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "head", "title"):
            self.skip_depth = max(self.skip_depth - 1, 0)
            return
        if tag in _VOID or not self.stack:
            return
        if self.stack.pop():
            self.hidden_depth = max(self.hidden_depth - 1, 0)
        if tag in _BLOCK and not self.hidden_depth:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.hidden_depth:
            if data.strip():
                self.hidden_found = True
            return
        self.parts.append(data)


def html_to_text(markup: str) -> tuple[str, bool]:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML: fall back to tag stripping
        text = re.sub(r"<[^>]+>", " ", markup)
        return normalize_ws(html.unescape(text)), False
    return normalize_ws("".join(parser.parts)), parser.hidden_found


def normalize_ws(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t​‌‍﻿]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def strip_quoted(text: str) -> str:
    """Remove quoted history (for triage input only)."""
    out = []
    for line in text.splitlines():
        if re.match(r"^\s*On .{5,200} wrote:\s*$", line) or re.match(r"^-{2,}\s*Original Message\s*-{2,}", line, re.I) \
                or re.match(r"^\s*From: .+", line) and out and out[-1].strip() == "":
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def cap_text(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode("utf-8", errors="ignore") + "\n[…truncated]"


def parse_auth_results(values: list[str]) -> tuple[str, str, str, list[str]]:
    """Use the first Authentication-Results added by Gmail itself."""
    for value in values:
        authserv = value.split(";", 1)[0].strip().lower()
        if authserv != TRUSTED_AUTHSERV:
            continue
        spf = dkim = dmarc = "none"
        domains: list[str] = []
        for clause in value.split(";")[1:]:
            clause = clause.strip()
            m = re.match(r"(spf|dkim|dmarc)\s*=\s*(\w+)", clause, re.I)
            if not m:
                continue
            kind, result = m.group(1).lower(), m.group(2).lower()
            norm = "pass" if result == "pass" else ("fail" if result in ("fail", "softfail", "permerror",
                                                                          "temperror", "neutral", "policy") else "none")
            if kind == "dkim":
                if norm == "pass":
                    dkim = "pass"
                    d = re.search(r"header\.(?:i|d)\s*=\s*@?([A-Za-z0-9.-]+)", clause)
                    if d:
                        domains.append(d.group(1).lower())
                elif dkim != "pass":
                    dkim = norm
            elif kind == "spf":
                spf = norm
            else:
                dmarc = norm
        return spf, dkim, dmarc, sorted(set(domains))
    return "none", "none", "none", []


def _walk(part: dict[str, Any]):
    yield part
    for sub in part.get("parts") or []:
        yield from _walk(sub)


def _decode(part: dict[str, Any]) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    raw = b64url_decode(data)
    charset = "utf-8"
    for h in part.get("headers") or []:
        if h.get("name", "").lower() == "content-type":
            m = re.search(r"charset=\"?([\w-]+)", h.get("value", ""), re.I)
            if m:
                charset = m.group(1)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _date(value: str | None, internal_ms: str | None) -> datetime | None:
    if internal_ms and str(internal_ms).isdigit():
        return datetime.fromtimestamp(int(internal_ms) / 1000, UTC)
    if value:
        try:
            dt = parsedate_to_datetime(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return None
    return None


def parse_message(msg: dict[str, Any], *, body_cap_bytes: int = 200_000) -> ParsedEmail:
    payload = msg.get("payload") or {}
    raw_headers: list[tuple[str, str]] = [(h.get("name", ""), h.get("value", "")) for h in payload.get("headers") or []]
    headers: dict[str, str] = {}
    for name, value in raw_headers:
        canonical = next((k for k in KEEP_HEADERS if k.lower() == name.lower()), None)
        if canonical and canonical not in headers:
            headers[canonical] = value[:2000]
    auth_values = [v for n, v in raw_headers if n.lower() == "authentication-results"]

    plain, markup, attachments = [], [], []
    for part in _walk(payload):
        mime = (part.get("mimeType") or "").lower()
        filename = part.get("filename") or ""
        body = part.get("body") or {}
        if filename or body.get("attachmentId"):
            attachments.append(ParsedAttachment(filename=filename[:255] or "attachment", mime_type=mime,
                                                size=int(body.get("size") or 0),
                                                attachment_id=body.get("attachmentId")))
        elif mime == "text/plain":
            plain.append(_decode(part))
        elif mime == "text/html":
            markup.append(_decode(part))

    hidden = False
    if markup:
        html_text, hidden = html_to_text("\n".join(markup))
    if plain and any(p.strip() for p in plain):
        body_text = normalize_ws("\n".join(plain))
    else:
        body_text = html_text if markup else ""

    from_name, from_addr = parseaddr(headers.get("From", ""))
    labels = list(msg.get("labelIds") or [])
    return ParsedEmail(
        provider_message_id=msg["id"],
        provider_thread_id=msg.get("threadId") or msg["id"],
        headers=headers,
        from_name=from_name[:320],
        from_address=from_addr.strip().lower()[:320],
        to=[a.lower() for _, a in getaddresses([headers.get("To", "")]) if a],
        cc=[a.lower() for _, a in getaddresses([headers.get("Cc", "")]) if a],
        reply_to=(parseaddr(headers["Reply-To"])[1].lower() or None) if headers.get("Reply-To") else None,
        subject=headers.get("Subject", "")[:998],
        sent_at=_date(headers.get("Date"), msg.get("internalDate")),
        rfc_message_id=headers.get("Message-ID"),
        body_text=cap_text(body_text, body_cap_bytes),
        snippet=html.unescape(msg.get("snippet") or "")[:300],
        labels=labels,
        gmail_category=next((CATEGORY_LABELS[l] for l in labels if l in CATEGORY_LABELS), None),
        attachments=attachments,
        hidden_text_detected=hidden,
        is_self_sent=headers.get(OUTBOUND_HEADER, "").strip().lower() == OUTBOUND_VALUE,
        **dict(zip(("auth_spf", "auth_dkim", "auth_dmarc", "dkim_domains"), parse_auth_results(auth_values))),
    )
