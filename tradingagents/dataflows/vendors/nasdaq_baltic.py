"""Nasdaq Baltic company announcements: company news and managers' transactions.

Yahoo tags no news to Baltic listings. What a Baltic company discloses (results,
monthly turnover, dividends, AGM decisions, managers' trades under MAR article
19) goes out through Nasdaq's company-announcement service, which nasdaqbaltic.com
also reads. Its JSON endpoint takes the issuer's name, not the share code, so a
share is looked up in the Baltic registry first (``dataflows/baltic``).

The endpoint ignores date filters and returns an issuer's announcements newest
first, so this vendor pages back until it passes the start of the window and
keeps what falls inside it. Each announcement's text sits on its own page
(``#view-body``); it is fetched once and kept in the cache, since a published
announcement never changes, and a failed fetch leaves the headline alone.

A ticker that is not a Baltic listing, or not in the registry, raises
NoMarketDataError, so a chain such as ``nasdaq_baltic,yfinance`` moves on to the
next vendor.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path

import requests

from tradingagents import __version__
from tradingagents.dataflows import proxy
from tradingagents.dataflows.baltic import BalticInstrument, is_baltic, lookup
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import get_current_date, in_window, to_utc
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

API_URL = "https://api.news.eu.nasdaq.com/news/query.action"
SOURCE = "Nasdaq Baltic"
_PAGE_SIZE = 100
_MAX_PAGES = 20          # 2,000 announcements: back to 2019 for the busiest issuers
_TIMEOUT = 20
_PAGE_TTL_SECONDS = 300  # pages of the newest announcements change as issuers publish
_BODY_CHARS = 1500       # of each announcement's text put into the report
_INSIDER_LOOKBACK_DAYS = 365
_HEADERS = {
    "User-Agent": f"TradingAgents/{__version__} (+https://github.com/TauricResearch/TradingAgents)",
    "Accept": "application/json",
}

# MAR article 19 notifications: the category Nasdaq files them under, and the
# headline issuers give them when the category is a generic one.
_MANAGERS_CATEGORY = re.compile(r"transactions? (concluded )?by managers", re.IGNORECASE)
_MANAGERS_HEADLINE = re.compile(
    r"manager'?s?'? (related party )?transactions?"
    r"|transactions? (concluded )?by (a )?(person|manager)"
    r"|person(s)? discharging managerial responsibilit"
    r"|closely associated with (a )?(manager|person)",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# The share and its issuer
# --------------------------------------------------------------------------- #

def _instrument(ticker: str) -> BalticInstrument:
    canonical = normalize_symbol(ticker)
    instrument = lookup(canonical)
    if instrument is not None:
        return instrument
    if is_baltic(canonical):
        raise NoMarketDataError(
            ticker, canonical,
            "not in the Baltic registry; add it to instruments.toml to read its announcements",
        )
    raise NoMarketDataError(ticker, canonical, "not a Nasdaq Baltic listing")


# --------------------------------------------------------------------------- #
# Announcement lists
# --------------------------------------------------------------------------- #

def _parse_time(value: str | None) -> datetime | None:
    """``2026-10-01 13:00:00 +0000`` (the default form) as an aware datetime."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            return to_utc(datetime.strptime(value.strip(), fmt))
        except ValueError:
            continue
    return None


def _looks_like_json(response) -> bool:
    return (response.text or "").lstrip()[:1] in ("{", "[")


def _fetch_page(issuer: str, start: int) -> list[dict]:
    params = {
        "type": "json",
        "company": issuer,
        "limit": _PAGE_SIZE,
        "start": start,
        "dir": "DESC",
        "displayLanguage": "en",
        "timeZone": "UTC",
        "showAttachments": "true",
        "showCnsSpecific": "true",
        "showCompany": "true",
        "countResults": "false",
    }
    try:
        response = proxy.get(API_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT,
                             expect=_looks_like_json)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise VendorUnavailableError(
            f"Nasdaq announcement service request failed ({status or type(exc).__name__})"
        ) from exc
    except ValueError as exc:
        # A bot check answers with an HTML page instead of JSON.
        raise VendorUnavailableError("Nasdaq announcement service returned no JSON") from exc
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, dict):
        raise VendorUnavailableError("Nasdaq announcement service returned an unexpected payload")
    items = results.get("item") or []
    return items if isinstance(items, list) else [items]


@lru_cache(maxsize=256)
def _cached_page(issuer: str, start: int, bucket: int) -> tuple[dict, ...]:
    """One page, reused for ``_PAGE_TTL_SECONDS`` (``bucket`` is the time slot)."""
    return tuple(_fetch_page(issuer, start))


def _announcement(item: dict) -> dict | None:
    released = _parse_time(item.get("releaseTime") or item.get("published"))
    headline = (item.get("headline") or "").strip()
    if released is None or not headline:
        return None
    attachments = [
        a.get("fileName") for a in item.get("attachment") or []
        if isinstance(a, dict) and a.get("fileName")
    ]
    return {
        "id": str(item.get("disclosureId") or ""),
        "headline": headline,
        "category": (item.get("cnsCategory") or "").strip(),
        "released": released,
        "url": item.get("messageUrl") or "",
        "language": item.get("language") or "",
        "attachments": attachments,
    }


def announcements(
    issuer: str, start_dt: datetime, end_dt: datetime
) -> tuple[list[dict], datetime | None]:
    """The issuer's announcements in ``[start, end + 1 day)``, newest first.

    Also returns the date coverage starts when the pages did not reach back to
    ``start`` (the issuer's history begins later, or there were more pages than
    are read), else None, so a caller can tell an unread window from an empty one.
    """
    bucket = int(time.time() // _PAGE_TTL_SECONDS)
    start_utc = to_utc(start_dt)
    found: list[dict] = []
    oldest: datetime | None = None
    for page in range(_MAX_PAGES):
        items = _cached_page(issuer, page * _PAGE_SIZE, bucket)
        if page == 0 and not items:
            raise NoMarketDataError(
                issuer, issuer,
                "Nasdaq files no announcements under this issuer name; check 'issuer' in the registry",
            )
        parsed = [a for a in (_announcement(i) for i in items) if a is not None]
        found.extend(a for a in parsed if in_window(a["released"], start_dt, end_dt))
        if parsed:
            page_oldest = min(a["released"] for a in parsed)
            oldest = page_oldest if oldest is None else min(oldest, page_oldest)
        if oldest is not None and oldest <= start_utc:
            return found, None
        if len(items) < _PAGE_SIZE:
            break
    return found, oldest


# --------------------------------------------------------------------------- #
# Announcement text
# --------------------------------------------------------------------------- #

class _BodyText(HTMLParser):
    """Text of the ``<div id="view-body">`` on an announcement page."""

    _BREAKS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0  # nesting inside view-body; 0 = outside
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.depth:
            if tag == "div":
                self.depth += 1
            if tag in self._BREAKS:
                self.parts.append("\n")
            elif tag == "td":
                self.parts.append(" | ")
        elif tag == "div" and dict(attrs).get("id") == "view-body":
            self.depth = 1

    def handle_startendtag(self, tag, attrs):
        if self.depth and tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.depth and tag == "div":
            self.depth -= 1
        if self.depth and tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in raw.splitlines()]
        out, blank = [], False
        for line in lines:
            if line:
                out.append(line)
                blank = False
            elif not blank and out:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def body_text(html: str) -> str:
    """The announcement's text from its page's HTML; empty when the page has none."""
    parser = _BodyText()
    parser.feed(html)
    parser.close()
    return parser.text()


def _body(announcement: dict) -> str:
    """The announcement's text, from the cache or its page; empty when unreachable."""
    if not announcement["url"] or not announcement["id"].isdigit():
        return ""
    path = Path(get_config()["data_cache_dir"]) / "nasdaq_baltic" / f"{announcement['id']}.txt"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            pass
    try:
        response = proxy.get(
            announcement["url"], headers={**_HEADERS, "Accept": "text/html"}, timeout=_TIMEOUT,
            expect=lambda r: 'id="view-body"' in r.text,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.info("Announcement %s text not fetched: %s", announcement["id"], exc)
        return ""
    text = body_text(response.text)
    if text:  # an empty body is most likely a bot check, not the announcement
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_file(path, lambda temp: Path(temp).write_text(text, encoding="utf-8"))
    return text


def _trim(text: str, limit: int = _BODY_CHARS) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut} [...]"


def _format(announcement: dict, with_body: bool) -> str:
    when = announcement["released"].strftime("%Y-%m-%d %H:%M UTC")
    meta = " · ".join(p for p in (when, announcement["category"], SOURCE) if p)
    out = f"### {announcement['headline']}\n{meta}\n"
    if with_body:
        text = _body(announcement)
        if text:
            out += f"{_trim(text)}\n"
    if announcement["attachments"]:
        out += f"Attachments: {', '.join(announcement['attachments'])}\n"
    if announcement["url"]:
        out += f"Link: {announcement['url']}\n"
    return out + "\n"


def is_managers_transaction(announcement: dict) -> bool:
    """Whether an announcement notifies a manager's dealing (MAR article 19)."""
    return bool(
        _MANAGERS_CATEGORY.search(announcement["category"])
        or _MANAGERS_HEADLINE.search(announcement["headline"])
    )


# --------------------------------------------------------------------------- #
# Vendor methods
# --------------------------------------------------------------------------- #

def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """The company's Nasdaq Baltic announcements published in the window, newest first."""
    instrument = _instrument(ticker)
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    found, covered_from = announcements(instrument.issuer, start_dt, end_dt)
    subject = f"{instrument.ticker} ({instrument.name})"
    gap = (
        f"{SOURCE} announcements read back only to {covered_from:%Y-%m-%d}, so earlier "
        f"days of the window were not observed"
        if covered_from is not None else ""
    )

    if not found:
        if gap:
            return f"<{gap}; this is not an absence of news for {subject}>"
        return (
            f"No company announcements from {instrument.issuer} on {SOURCE} between "
            f"{start_date} and {end_date}. This is the channel through which listed "
            f"companies publish regulated information, and none was published in the window."
        )

    limit = get_config()["news_article_limit"]
    shown = found[:limit]
    more = len(found) - len(shown)
    body = "".join(_format(a, with_body=True) for a in shown)
    header = (
        f"## {subject} News, from {start_date} to {end_date}:\n"
        f"Company announcements published on {SOURCE} (official disclosures by "
        f"{instrument.issuer}), newest first.\n\n"
    )
    footer = f"({more} older announcements in the window not shown.)\n" if more > 0 else ""
    if gap:
        footer += f"({gap}.)\n"
    return header + body + footer


def get_insider_transactions(ticker: str, as_of_date: str | None = None) -> str:
    """Managers' transactions the company notified in the year to ``as_of_date``.

    Each notification is dated by its publication, so a past run sees only
    those public on its date. The trade details (who, price, volume) are in the
    notification's text or its PDF attachment.
    """
    instrument = _instrument(ticker)
    end_date = as_of_date or get_current_date()
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=_INSIDER_LOOKBACK_DAYS)
    found, covered_from = announcements(instrument.issuer, start_dt, end_dt)
    notices = [a for a in found if is_managers_transaction(a)]

    header = (
        f"# Insider Transactions for {instrument.ticker} ({instrument.name})\n"
        f"# Point-in-time as of: {end_date}\n"
        f"# Managers' transactions notified on {SOURCE} under MAR article 19, "
        f"{start_dt:%Y-%m-%d} to {end_date}, newest first. Trade details are in "
        f"each notification's text or its attachment.\n"
    )
    if covered_from is not None:
        header += f"# Notifications read back only to {covered_from:%Y-%m-%d}.\n"
    header += "\n"
    if not notices:
        return header + f"No managers' transactions were notified by {instrument.issuer} in this period.\n"
    return header + "".join(_format(a, with_body=True) for a in notices)


def clear_cache() -> None:
    """Forget cached announcement pages (tests, or a run that must see the latest)."""
    _cached_page.cache_clear()


__all__ = [
    "API_URL",
    "announcements",
    "body_text",
    "clear_cache",
    "get_insider_transactions",
    "get_news",
    "is_managers_transaction",
]
