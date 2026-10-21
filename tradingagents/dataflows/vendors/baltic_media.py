"""Press coverage of Baltic companies, from Google News.

Baltic business news (Verslo žinios, Delfi, 15min, LRT, ERR, LSM and the like)
is written in the market's own language and rarely tagged to a share, so this
vendor searches Google News for the company's names in two editions: the
market's language (Lithuanian, Latvian, Estonian) and English.

A company's name can also be a common word ("apranga" is Lithuanian for
clothing), so a local headline counts only when it carries one of the
registry's ``press_names`` (the quoted company name, say); an English headline
when it carries one of the company's names.

Searches ask Google for the analysis window (``after:``/``before:``) and the
results are cut to it again by publication time, so a past run reads only what
had been published by its date. Headlines that repeat a Nasdaq announcement
(wire copies on GlobeNewswire, Yahoo and the like) are dropped by the caller.
"""

from __future__ import annotations

import email.utils
import html
import logging
import re
from datetime import datetime, timedelta

import requests

from tradingagents import __version__
from tradingagents.dataflows import proxy
from tradingagents.dataflows.baltic import BalticInstrument
from tradingagents.dataflows.date_window import in_window, to_utc
from tradingagents.dataflows.errors import VendorUnavailableError

logger = logging.getLogger(__name__)

RSS_URL = "https://news.google.com/rss/search"
_TIMEOUT = 20
_HEADERS = {
    "User-Agent": f"TradingAgents/{__version__} (+https://github.com/TauricResearch/TradingAgents)",
    "Accept": "application/rss+xml, application/xml",
}
# Google News edition (hl, gl, ceid) for each Baltic exchange's language.
EDITIONS = {
    "Vilnius": ("lt", "LT", "LT:lt"),
    "Riga": ("lv", "LV", "LV:lv"),
    "Tallinn": ("et", "EE", "EE:et"),
}
ENGLISH = ("en-US", "US", "US:en")

_ITEM = re.compile(r"<item>(.*?)</item>", re.DOTALL)
_SOURCE = re.compile(r'<source(?: url="([^"]*)")?>(.*?)</source>', re.DOTALL)


def _tag(block: str, name: str) -> str:
    match = re.search(rf"<{name}(?:\s[^>]*)?>(.*?)</{name}>", block, re.DOTALL)
    if not match:
        return ""
    text = match.group(1).strip()
    if text.startswith("<![CDATA[") and text.endswith("]]>"):
        text = text[9:-3]
    return html.unescape(text).strip()


def parse_rss(xml: str) -> list[dict]:
    """Items of a Google News RSS feed: title (without the source suffix), source, link, date."""
    items = []
    for block in _ITEM.findall(xml):
        title = _tag(block, "title")
        source_match = _SOURCE.search(block)
        source = html.unescape(source_match.group(2)).strip() if source_match else ""
        source_url = source_match.group(1) if source_match and source_match.group(1) else ""
        # Google appends " - <source>" to every title.
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].rstrip()
        published = None
        raw_date = _tag(block, "pubDate")
        if raw_date:
            try:
                published = to_utc(email.utils.parsedate_to_datetime(raw_date))
            except (TypeError, ValueError):
                published = None
        if title:
            items.append({
                "title": title,
                "source": source,
                "source_url": source_url,
                "link": _tag(block, "link"),
                "published": published,
            })
    return items


def _query(names: tuple[str, ...], start_dt: datetime, end_dt: datetime) -> str:
    terms = " OR ".join(f'"{name}"' for name in names)
    # ``before:`` is exclusive: the day after the window's end keeps its last day.
    return (
        f"{terms} after:{start_dt:%Y-%m-%d} "
        f"before:{end_dt + timedelta(days=1):%Y-%m-%d}"
    )


def _search(query: str, edition: tuple[str, str, str]) -> list[dict]:
    hl, gl, ceid = edition
    try:
        response = proxy.get(
            RSS_URL,
            params={"q": query, "hl": hl, "gl": gl, "ceid": ceid},
            headers=_HEADERS,
            timeout=_TIMEOUT,
            expect=lambda r: "<rss" in (r.text or "")[:500],
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise VendorUnavailableError(
            f"Google News request failed ({status or type(exc).__name__})"
        ) from exc
    if "<rss" not in response.text[:500]:
        # A consent or bot-check page instead of the feed.
        raise VendorUnavailableError("Google News returned no RSS feed")
    return parse_rss(response.text)


def _mentions(title: str, markers: tuple[str, ...]) -> bool:
    return any(marker in title for marker in markers)


def press_coverage(
    instrument: BalticInstrument, start_dt: datetime, end_dt: datetime,
) -> list[dict]:
    """Headlines about the company published in ``[start, end + 1 day)``, newest first.

    Raises VendorUnavailableError when neither edition answers.
    """
    names = instrument.search_names
    query = _query(names, start_dt, end_dt)
    searches = []
    local = EDITIONS.get(instrument.market)
    if local:
        searches.append((local, instrument.press_markers))
    searches.append((ENGLISH, names))

    found: dict[str, dict] = {}
    failures = []
    for edition, markers in searches:
        try:
            items = _search(query, edition)
        except VendorUnavailableError as exc:
            failures.append(exc)
            continue
        for item in items:
            if not in_window(item["published"], start_dt, end_dt):
                continue
            if not _mentions(item["title"], markers):
                continue
            key = _normalize(item["title"])
            if key not in found:
                found[key] = item
    if len(failures) == len(searches):
        raise failures[0]
    return sorted(found.values(), key=lambda i: i["published"] or to_utc(end_dt), reverse=True)


def _normalize(title: str) -> str:
    return re.sub(r"\W+", " ", title).strip().casefold()


def repeats_announcement(title: str, headlines: list[str]) -> bool:
    """Whether a headline is a wire copy of one of the company's own announcements."""
    key = _normalize(title)
    return any(key == _normalize(h) for h in headlines if h)


def format_coverage(items: list[dict]) -> str:
    out = ""
    for item in items:
        when = item["published"].strftime("%Y-%m-%d %H:%M UTC") if item["published"] else "undated"
        source = item["source"] or "Google News"
        out += f"### {item['title']}\n{when} · {source}\n"
        if item["link"]:
            out += f"Link: {item['link']}\n"
        out += "\n"
    return out
