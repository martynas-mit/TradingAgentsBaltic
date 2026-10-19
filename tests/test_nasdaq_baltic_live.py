"""Live check of the Baltic vendors. Reaches the network: run with -m integration."""

from datetime import date, timedelta

import pytest

from tradingagents.dataflows.vendors import nasdaq_baltic as nb

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("ticker", ["APG1L", "ROE1L"])
def test_recent_announcements_come_back_with_their_text(ticker):
    end = date.today()
    start = end - timedelta(days=90)
    report = nb.get_news(ticker, f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}")
    print(report[:3000])
    assert report.startswith(f"## {ticker}.VS")
    assert "Link: https://view.news.eu.nasdaq.com/" in report


def test_managers_transactions_for_the_past_year():
    report = nb.get_insider_transactions("ROE1L")
    print(report[:3000])
    assert report.startswith("# Insider Transactions for ROE1L.VS")


def test_press_coverage_and_announcements_together():
    from tradingagents.dataflows.vendors import baltic

    end = date.today()
    start = end - timedelta(days=60)
    report = baltic.get_news("ROE1L", f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}")
    print(report[-3000:])
    assert "Press coverage" in report or "No press headlines" in report
