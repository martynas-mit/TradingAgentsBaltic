"""The ``baltic`` vendor: every Baltic source for a share, in one answer.

The router serves a tool from the first vendor in a chain that answers, so it
cannot merge two sources. For company news this vendor does: the issuer's own
Nasdaq Baltic announcements first, then the press coverage Google News finds
in the market's language and in English, without wire copies of the
announcements. Either source may be unreachable; the other is still served,
with a note. Insider transactions come from the announcements alone.

Statements come from the company's ESEF annual reports (filings.xbrl.org, by
the LEI in the registry), dated by when each was filed. Quarterly statements
are not filed in ESEF, so a quarterly income statement request adds the text
of the latest interim report announcements.

A ticker that is not a registered Baltic share raises NoMarketDataError before
any request, so the chain ``baltic,yfinance`` serves every other ticker from
Yahoo as before.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import baltic_media as media, esef, nasdaq_baltic as nasdaq

get_insider_transactions = nasdaq.get_insider_transactions


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Nasdaq Baltic announcements, then press coverage, for the window."""
    instrument = nasdaq._instrument(ticker)
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    announcements_error: VendorUnavailableError | None = None
    headlines: list[str] = []
    try:
        announcements = nasdaq.get_news(ticker, start_date, end_date)
        found, _ = nasdaq.announcements(instrument.issuer, start_dt, end_dt)  # cached pages
        headlines = [a["headline"] for a in found]
    except VendorUnavailableError as exc:
        announcements_error = exc
        announcements = (
            f"<{nasdaq.SOURCE} announcements unavailable for {start_date}..{end_date} "
            f"({exc}), so this is not an absence of company announcements>\n"
        )

    try:
        coverage = [
            item for item in media.press_coverage(instrument, start_dt, end_dt)
            if not media.repeats_announcement(item["title"], headlines)
        ]
    except VendorUnavailableError as exc:
        if announcements_error is not None:
            raise announcements_error from exc
        press = (
            f"<Press coverage unavailable for {start_date}..{end_date} ({exc}), "
            f"so this is not an absence of press coverage>\n"
        )
    else:
        limit = get_config()["news_article_limit"]
        shown = coverage[:limit]
        if shown:
            press = (
                f"## Press coverage of {instrument.name}, from {start_date} to {end_date}:\n"
                f"Headlines found by Google News in local and English-language media, "
                f"newest first; local headlines are in the original language.\n\n"
                + media.format_coverage(shown)
            )
            if len(coverage) > len(shown):
                press += f"({len(coverage) - len(shown)} older headlines in the window not shown.)\n"
        else:
            press = (
                f"No press headlines about {instrument.name} found by Google News "
                f"between {start_date} and {end_date}.\n"
            )

    return f"{announcements.rstrip()}\n\n{press}"


# --------------------------------------------------------------------------- #
# Statements: annual from ESEF, interim from the report announcements
# --------------------------------------------------------------------------- #

_INTERIM_CATEGORIES = ("Interim information", "Half-Yearly information")
_INTERIM_HEADLINE = re.compile(
    r"interim (report|financial)|financial statements? for (the )?(q[1-4]|\d+ months|h[12]|first|half)"
    r"|results? for (the )?(q[1-4]|\d+ months|first|second|third|half)|unaudited",
    re.IGNORECASE,
)
_INTERIM_LOOKBACK_DAYS = 200
_INTERIM_REPORTS = 2
_INTERIM_CHARS = 3000


def _interim_reports(instrument, as_of_date: str) -> str:
    """The latest interim report announcements' text, public by ``as_of_date``."""
    end_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=_INTERIM_LOOKBACK_DAYS)
    try:
        found, _ = nasdaq.announcements(instrument.issuer, start_dt, end_dt)
    except VendorUnavailableError as exc:
        return f"<Interim report announcements unavailable ({exc})>\n"
    reports = [
        a for a in found
        if a["category"] in _INTERIM_CATEGORIES or _INTERIM_HEADLINE.search(a["headline"])
    ][:_INTERIM_REPORTS]
    if not reports:
        return (
            f"No interim report announcements from {instrument.issuer} in the "
            f"{_INTERIM_LOOKBACK_DAYS} days to {as_of_date}.\n"
        )
    out = (
        f"## Latest interim reports ({nasdaq.SOURCE} announcements, unaudited unless "
        f"stated)\nQuarterly statements are not filed in ESEF; these are the company's "
        f"own summaries. Full statements are in the attached PDF.\n\n"
    )
    for report in reports:
        when = report["released"].strftime("%Y-%m-%d")
        out += f"### {report['headline']} (published {when})\n"
        text = nasdaq._body(report)
        out += f"{nasdaq._trim(text, _INTERIM_CHARS)}\n" if text else "(text not retrieved)\n"
        if report["attachments"]:
            out += f"Attachments: {', '.join(report['attachments'])}\n"
        if report["url"]:
            out += f"Link: {report['url']}\n"
        out += "\n"
    return out


def _statement(kind: str, ticker: str, freq: str, as_of_date: str | None) -> str:
    instrument = nasdaq._instrument(ticker)
    as_of = as_of_date or get_current_date()
    label = f"{instrument.ticker} ({instrument.name})"
    interim = kind == "income" and str(freq).lower().startswith("q")
    annual = None
    missing: NoMarketDataError | None = None
    if instrument.lei:
        try:
            annual = esef.statement(instrument.lei, kind, label, as_of)
        except NoMarketDataError as exc:
            missing = exc
    else:
        missing = NoMarketDataError(ticker, instrument.ticker, "no LEI in the Baltic registry")

    if annual is None and not interim:
        raise missing
    parts = [annual] if annual else [
        f"# {esef.STATEMENTS[kind][0]} for {label}\n# Point-in-time as of: {as_of}\n"
        f"<No ESEF annual report available: {missing.detail}>\n"
    ]
    if interim:
        parts.append(_interim_reports(instrument, as_of))
    elif str(freq).lower().startswith("q"):
        parts.append(
            "Quarterly figures are not filed in ESEF; the interim report summaries are "
            "served with the income statement.\n"
        )
    if instrument.sector == "bank":
        parts.append(
            "Note: a bank's statements differ from a trading company's: read net interest "
            "and fee income, loans and deposits rather than gross profit and inventories.\n"
        )
    return "\n".join(parts)


def get_income_statement(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    return _statement("income", ticker, freq, as_of_date)


def get_balance_sheet(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    return _statement("balance", ticker, freq, as_of_date)


def get_cashflow(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    return _statement("cashflow", ticker, freq, as_of_date)
