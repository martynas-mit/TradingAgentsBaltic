"""Press coverage from Google News, and the ``baltic`` vendor that merges it with announcements.

The RSS is a recorded shape of news.google.com/rss/search (October 2026).
"""

from datetime import datetime
from unittest import mock

import pytest
import requests

from tests.test_nasdaq_baltic import APRANGA, BODY_HTML, _response
from tradingagents.dataflows import router
from tradingagents.dataflows.baltic import lookup
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import baltic, baltic_media as media, nasdaq_baltic as nb

pytestmark = pytest.mark.unit


def _rss_item(title, source, when, site="https://www.vz.lt"):
    return (
        f"<item><title>{title} - {source}</title>"
        f"<link>https://news.google.com/rss/articles/CB{abs(hash(title))}?oc=5</link>"
        f'<guid isPermaLink="false">x</guid><pubDate>{when}</pubDate>'
        f"<description>&lt;a href=&quot;x&quot;&gt;{title}&lt;/a&gt;</description>"
        f'<source url="{site}">{source}</source></item>'
    )


def _rss(*items):
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><rss version="2.0" '
        'xmlns:media="http://search.yahoo.com/mrss/"><channel><generator>NFE/5.0</generator>'
        "<title>search</title>" + "".join(items) + "</channel></rss>"
    )


LT_FEED = _rss(
    _rss_item("„Aprangos“ grupės apyvarta rugsėjį augo 10,9 proc.", "15min.lt",
              "Fri, 02 Oct 2026 05:00:00 GMT", "https://www.15min.lt"),
    _rss_item("Netikėta Melanios apranga priėmime išmušė iš vėžių", "Delfi",
              "Fri, 25 Sep 2026 07:00:00 GMT", "https://www.delfi.lt"),
    _rss_item("„Apranga“ dividendams siūlo skirti 14,9 mln. Eur, 0,27 Eur akcijai", "vz.lt",
              "Sat, 04 Apr 2026 07:00:00 GMT"),
    _rss_item("„Aprangos“ vadovas: rudens sezonas prasidėjo anksčiau", "vz.lt",
              "Mon, 19 Oct 2026 06:00:00 GMT"),
)
EN_FEED = _rss(
    _rss_item("Turnover of Apranga Group in September 2026", "GlobeNewswire",
              "Thu, 01 Oct 2026 13:00:00 GMT", "https://www.globenewswire.com"),
    _rss_item("Apranga September Retail Turnover At EUR 34.60 Million, Up 10.9% YoY",
              "TradingView", "Thu, 01 Oct 2026 14:00:00 GMT", "https://www.tradingview.com"),
    _rss_item("„Aprangos“ grupės apyvarta rugsėjį augo 10,9 proc.", "15min.lt",
              "Fri, 02 Oct 2026 05:00:00 GMT", "https://www.15min.lt"),
)


class FakeWeb:
    """Nasdaq's API and pages, and Google News, by URL."""

    def __init__(self, lt=LT_FEED, en=EN_FEED, nasdaq_items=APRANGA, nasdaq_up=True, google_up=True):
        self.lt, self.en = lt, en
        self.nasdaq_items = nasdaq_items
        self.nasdaq_up, self.google_up = nasdaq_up, google_up
        self.google_calls: list[dict] = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        if url == media.RSS_URL:
            self.google_calls.append(params)
            if not self.google_up:
                raise requests.ConnectionError("google down")
            return _response(text=self.lt if params["hl"] == "lt" else self.en)
        if not self.nasdaq_up:
            raise requests.ConnectionError("nasdaq down")
        if url == nb.API_URL:
            start = int(params["start"])
            return _response(payload={"results": {"item": self.nasdaq_items[start:start + 100]}})
        return _response(text=BODY_HTML)


@pytest.fixture(autouse=True)
def _fresh(tmp_path):
    nb.clear_cache()
    with run_config({"data_cache_dir": str(tmp_path), "news_article_limit": 20}):
        yield
    nb.clear_cache()


def _web(fake):
    return mock.patch("requests.get", side_effect=fake)


class TestParse:
    def test_items_lose_the_source_suffix_and_keep_a_utc_date(self):
        items = media.parse_rss(LT_FEED)
        assert items[0]["title"] == "„Aprangos“ grupės apyvarta rugsėjį augo 10,9 proc."
        assert items[0]["source"] == "15min.lt"
        assert items[0]["published"] == datetime.fromisoformat("2026-10-02T05:00:00+00:00")
        assert items[0]["link"].startswith("https://news.google.com/rss/articles/")

    def test_an_item_without_a_date_is_undated(self):
        feed = _rss("<item><title>X - Y</title><source>Y</source></item>")
        assert media.parse_rss(feed)[0]["published"] is None


class TestPressCoverage:
    def test_local_headlines_need_the_company_marker_and_fall_in_the_window(self):
        fake = FakeWeb()
        with _web(fake):
            items = media.press_coverage(
                lookup("APG1L"), datetime(2026, 9, 20), datetime(2026, 10, 16))
        titles = [i["title"] for i in items]
        assert "„Aprangos“ grupės apyvarta rugsėjį augo 10,9 proc." in titles
        assert not any("Melanios" in t for t in titles)          # "apranga" = clothing
        assert not any("dividendams" in t for t in titles)       # before the window
        assert not any("rudens sezonas" in t for t in titles)    # after the window
        assert titles.count("„Aprangos“ grupės apyvarta rugsėjį augo 10,9 proc.") == 1
        dates = [i["published"] for i in items]
        assert dates == sorted(dates, reverse=True)  # newest first

    def test_searches_the_market_language_and_english_for_the_window(self):
        fake = FakeWeb()
        with _web(fake):
            media.press_coverage(lookup("APG1L"), datetime(2026, 9, 20), datetime(2026, 10, 16))
        assert [c["hl"] for c in fake.google_calls] == ["lt", "en-US"]
        query = fake.google_calls[0]["q"]
        assert '"Apranga"' in query
        assert query.endswith("after:2026-09-20 before:2026-10-17")

    def test_both_editions_down_is_vendor_unavailable(self):
        with _web(FakeWeb(google_up=False)), pytest.raises(VendorUnavailableError):
            media.press_coverage(lookup("APG1L"), datetime(2026, 9, 20), datetime(2026, 10, 16))

    def test_a_consent_page_instead_of_rss_is_vendor_unavailable(self):
        page = "<html><body>Before you continue to Google</body></html>"
        with _web(FakeWeb(lt=page, en=page)), pytest.raises(VendorUnavailableError, match="no RSS"):
            media.press_coverage(lookup("APG1L"), datetime(2026, 9, 20), datetime(2026, 10, 16))

    def test_wire_copies_of_announcements_are_recognized(self):
        assert media.repeats_announcement(
            "Turnover of Apranga Group in September 2026",
            ["Turnover of Apranga Group in September 2026"])
        assert not media.repeats_announcement(
            "Apranga September Retail Turnover At EUR 34.60 Million",
            ["Turnover of Apranga Group in September 2026"])


class TestBalticVendor:
    def test_announcements_then_press_without_wire_copies(self):
        with _web(FakeWeb()):
            report = baltic.get_news("APG1L", "2026-09-20", "2026-10-16")
        assert report.startswith("## APG1L.VS (APB Apranga) News, from 2026-09-20 to 2026-10-16:")
        press = report[report.index("## Press coverage of APB Apranga"):]
        assert "### „Aprangos“ grupės apyvarta rugsėjį augo 10,9 proc." in press
        assert "### Apranga September Retail Turnover" in press
        assert "### Turnover of Apranga Group in September 2026" not in press  # the wire copy

    def test_press_is_served_when_nasdaq_is_down(self):
        with _web(FakeWeb(nasdaq_up=False)):
            report = baltic.get_news("APG1L", "2026-09-20", "2026-10-16")
        assert report.startswith("<Nasdaq Baltic announcements unavailable")
        assert "## Press coverage of APB Apranga" in report

    def test_announcements_are_served_when_google_is_down(self):
        with _web(FakeWeb(google_up=False)):
            report = baltic.get_news("APG1L", "2026-09-20", "2026-10-16")
        assert "### Turnover of Apranga Group in September 2026" in report
        assert "<Press coverage unavailable" in report

    def test_both_down_is_vendor_unavailable(self):
        with _web(FakeWeb(nasdaq_up=False, google_up=False)), pytest.raises(VendorUnavailableError):
            baltic.get_news("APG1L", "2026-09-20", "2026-10-16")

    def test_no_press_in_the_window_says_so(self):
        with _web(FakeWeb(lt=_rss(), en=_rss())):
            report = baltic.get_news("APG1L", "2026-09-20", "2026-10-16")
        assert "No press headlines about APB Apranga found by Google News" in report

    def test_other_tickers_are_no_data_without_a_request(self):
        fake = FakeWeb()
        with _web(fake), pytest.raises(NoMarketDataError):
            baltic.get_news("NVDA", "2026-09-20", "2026-10-16")
        assert fake.google_calls == []

    def test_default_chain_uses_the_baltic_vendor(self):
        with _web(FakeWeb()):
            report = router.route_to_vendor("get_news", "APG1L", "2026-09-20", "2026-10-16")
        assert "## Press coverage of APB Apranga" in report

    def test_default_insider_chain_uses_the_announcements(self):
        with _web(FakeWeb()):
            report = router.route_to_vendor("get_insider_transactions", "APG1L", "2026-08-31")
        assert report.startswith("# Insider Transactions for APG1L.VS")
