"""The ``baltic`` vendor: every Baltic source for a share, in one answer.

The router serves a tool from the first vendor in a chain that answers, so it
cannot merge two sources. For company news this vendor does: the issuer's own
Nasdaq Baltic announcements first, then the press coverage Google News finds
in the market's language and in English, without wire copies of the
announcements. Either source may be unreachable; the other is still served,
with a note. Insider transactions come from the announcements alone.

A ticker that is not a registered Baltic share raises NoMarketDataError before
any request, so the chain ``baltic,yfinance`` serves every other ticker from
Yahoo as before.
"""

from __future__ import annotations

from datetime import datetime

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors import baltic_media as media, nasdaq_baltic as nasdaq

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
