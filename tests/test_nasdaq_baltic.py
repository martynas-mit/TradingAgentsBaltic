"""Nasdaq Baltic announcements vendor: news, managers' transactions, routing.

Responses are recorded shapes of api.news.eu.nasdaq.com/news/query.action and
of an announcement page on view.news.eu.nasdaq.com (October 2026).
"""

from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
import requests

from tradingagents.dataflows import router
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import nasdaq_baltic as nb

pytestmark = pytest.mark.unit

VIEW = "https://view.news.eu.nasdaq.com/view?id={}&lang=en&src=listed"


def _item(disclosure_id, released, headline, category="Other information", attachments=()):
    return {
        "disclosureId": disclosure_id,
        "categoryId": 170,
        "headline": headline,
        "language": "en",
        "languages": ["lt", "en"],
        "cnsCategory": category,
        "messageUrl": VIEW.format(f"b{disclosure_id}"),
        "releaseTime": f"{released} +0000",
        "published": f"{released} +0000",
        "market": "Main Market, Vilnius",
        "attachment": [
            {"mimetype": "application/pdf", "fileName": name,
             "attachmentUrl": f"https://attachment.news.eu.nasdaq.com/a{disclosure_id}"}
            for name in attachments
        ],
        "company": "Apranga",
        "cnsTypeId": "6",
    }


MANAGERS = "Notifications on transactions concluded by managers of the companies"

APRANGA = [
    _item(1466548, "2026-10-01 13:00:00", "Turnover of Apranga Group in September 2026"),
    _item(1462001, "2026-09-01 13:00:00", "Turnover of Apranga Group in August 2026"),
    _item(1460002, "2026-08-27 13:40:00", "Notification on manager's transactions", MANAGERS,
          ("Pranesimas.pdf",)),
    _item(1460001, "2026-08-27 13:40:00", "Notification on manager's related party transactions",
          MANAGERS, ("Notification.pdf",)),
    _item(1458000, "2026-08-03 13:00:00", "Turnover of Apranga Group in July 2026"),
    _item(1457000, "2026-07-29 13:00:00", "Apranga Group interim report for 6 months of 2026",
          "Half-Yearly information", ("Apranga 6m 2026.pdf",)),
    _item(1440000, "2026-06-10 06:45:00", "Notification on manager's related party transactions",
          MANAGERS),
]

BODY_HTML = """<html><head><title>Turnover of Apranga Group in September 2026</title></head>
<body><main>
  <div class="divider-8"></div>
  <div id="view-body"><p>Retail turnover (including VAT) of Apranga Group amounted to
  EUR 34.6&nbsp;million in September 2026 and increased by 10.9% compared to September 2025.</p>
  <p>Currently, the Apranga Group operates the chain of 174 stores.</p>
  <div><table><tr><td>Lithuania</td><td>103</td></tr></table></div>
  <p align="justify">Rimantas Perveneckas<br>&bdquo;Apranga&ldquo; Group General Manager</p></div>
  <div class="divider-8"></div>
</main><footer>Nasdaq Company News</footer></body></html>"""


def _response(*, payload=None, text=None, status=200):
    response = mock.Mock()
    response.status_code = status
    response.text = text if text is not None else ""
    if payload is not None:
        response.json.return_value = payload
    else:
        response.json.side_effect = ValueError("not JSON")
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"{status} error")
    else:
        response.raise_for_status.return_value = None
    return response


class FakeNasdaq:
    """Serves announcement pages by ``start`` and announcement pages by URL."""

    def __init__(self, items, body=BODY_HTML, page_size=nb._PAGE_SIZE):
        self.items = items
        self.body = body
        self.page_size = page_size
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        if url == nb.API_URL:
            start = int(params["start"])
            page = self.items[start:start + int(params["limit"])]
            return _response(payload={"results": {"item": page}})
        if self.body is None:
            raise requests.ConnectionError("view page unreachable")
        return _response(text=self.body)

    @property
    def api_calls(self):
        return [p for u, p in self.calls if u == nb.API_URL]

    @property
    def page_calls(self):
        return [u for u, _ in self.calls if u != nb.API_URL]


@pytest.fixture(autouse=True)
def _fresh(tmp_path):
    nb.clear_cache()
    with run_config({"data_cache_dir": str(tmp_path), "news_article_limit": 20}):
        yield
    nb.clear_cache()


def _patched(fake):
    return mock.patch.object(nb.requests, "get", side_effect=fake)


class TestNews:
    def test_window_holds_the_announcements_published_in_it(self):
        fake = FakeNasdaq(APRANGA)
        with _patched(fake):
            report = nb.get_news("APG1L", "2026-08-01", "2026-09-30")
        assert report.startswith("## APG1L.VS (APB Apranga) News, from 2026-08-01 to 2026-09-30:")
        assert "Turnover of Apranga Group in August 2026" in report
        assert "Turnover of Apranga Group in July 2026" in report
        assert "Notification on manager's transactions" in report
        # Order is newest first.
        assert report.index("in August 2026") < report.index("in July 2026")
        assert "Attachments: Pranesimas.pdf" in report
        assert "2026-09-01 13:00 UTC · Other information · Nasdaq Baltic" in report

    def test_nothing_published_after_the_window_reaches_the_report(self):
        fake = FakeNasdaq(APRANGA)
        with _patched(fake):
            report = nb.get_news("APG1L.VS", "2026-09-01", "2026-09-30")
        assert "### Turnover of Apranga Group in September 2026" not in report  # 2026-10-01
        assert "### Turnover of Apranga Group in August 2026" in report

    def test_announcement_text_is_included_and_cached(self, tmp_path):
        fake = FakeNasdaq(APRANGA)
        with _patched(fake):
            report = nb.get_news("APG1L", "2026-09-01", "2026-09-01")
            assert "EUR 34.6 million" in report
            assert "Lithuania | 103" in report
            assert "„Apranga“ Group General Manager" in report
            assert "Nasdaq Company News" not in report  # outside view-body
            first = len(fake.page_calls)
            nb.clear_cache()
            nb.get_news("APG1L", "2026-09-01", "2026-09-01")
        assert first == 1
        assert len(fake.page_calls) == 1  # second read came from the cache
        assert (tmp_path / "nasdaq_baltic" / "1462001.txt").exists()

    def test_unreachable_text_leaves_the_headline(self):
        fake = FakeNasdaq(APRANGA, body=None)
        with _patched(fake):
            report = nb.get_news("APG1L", "2026-09-01", "2026-09-01")
        assert "Turnover of Apranga Group in August 2026" in report
        assert "Link: https://view.news.eu.nasdaq.com" in report

    def test_a_bot_check_page_is_neither_shown_nor_cached(self, tmp_path):
        fake = FakeNasdaq(APRANGA, body="<html><body>Request unsuccessful. Incapsula</body></html>")
        with _patched(fake):
            report = nb.get_news("APG1L", "2026-09-01", "2026-09-01")
        assert "Incapsula" not in report
        assert not (tmp_path / "nasdaq_baltic").exists()

    def test_article_limit_caps_the_list_and_says_so(self):
        fake = FakeNasdaq(APRANGA)
        with _patched(fake), run_config({"news_article_limit": 2}):
            report = nb.get_news("APG1L", "2026-06-01", "2026-10-16")
        assert report.count("### ") == 2
        assert "(5 older announcements in the window not shown.)" in report

    def test_empty_window_with_full_coverage_is_a_real_absence(self):
        fake = FakeNasdaq(APRANGA)
        with _patched(fake):
            report = nb.get_news("APG1L", "2026-09-10", "2026-09-20")
        assert report.startswith("No company announcements from Apranga on Nasdaq Baltic")

    def test_window_before_the_history_is_unavailable_not_empty(self):
        fake = FakeNasdaq(APRANGA)
        with _patched(fake):
            report = nb.get_news("APG1L", "2019-01-01", "2019-01-31")
        assert report.startswith("<Nasdaq Baltic announcements read back only to 2026-06-10")
        assert "not an absence of news" in report

    def test_pages_back_until_the_window_start_and_no_further(self):
        newest = datetime(2026, 10, 16, 12, tzinfo=UTC)
        filler = [
            _item(2_000_000 + i, (newest - timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
                  f"Filler {i}")
            for i in range(nb._PAGE_SIZE)
        ]
        older = [_item(1, "2026-03-02 10:00:00", "Old news")] + [
            _item(10 + i, "2025-01-01 10:00:00", f"Older {i}") for i in range(nb._PAGE_SIZE)
        ]
        fake = FakeNasdaq(filler + older)
        with _patched(fake):
            report = nb.get_news("APG1L", "2026-03-01", "2026-03-05")
        assert "Old news" in report
        assert [p["start"] for p in fake.api_calls] == [0, nb._PAGE_SIZE]
        assert all(p["company"] == "Apranga" and p["timeZone"] == "UTC" for p in fake.api_calls)


class TestErrors:
    def test_a_non_baltic_ticker_is_no_data_without_a_request(self):
        fake = FakeNasdaq(APRANGA)
        with _patched(fake), pytest.raises(NoMarketDataError, match="not a Nasdaq Baltic listing"):
            nb.get_news("AAPL", "2026-09-01", "2026-09-30")
        assert fake.calls == []

    def test_a_baltic_ticker_missing_from_the_registry_says_where_to_add_it(self):
        with pytest.raises(NoMarketDataError, match="instruments.toml"):
            nb.get_news("IGN1L", "2026-09-01", "2026-09-30")

    def test_an_issuer_name_nasdaq_does_not_know_is_no_data(self):
        with _patched(FakeNasdaq([])), pytest.raises(NoMarketDataError, match="issuer name"):
            nb.get_news("APG1L", "2026-09-01", "2026-09-30")

    def test_a_failed_request_is_vendor_unavailable(self):
        def down(*a, **k):
            raise requests.ConnectionError("down")

        with mock.patch.object(nb.requests, "get", side_effect=down), \
                pytest.raises(VendorUnavailableError, match="ConnectionError"):
            nb.get_news("APG1L", "2026-09-01", "2026-09-30")

    def test_an_http_error_is_vendor_unavailable(self):
        with mock.patch.object(nb.requests, "get", return_value=_response(status=503)), \
                pytest.raises(VendorUnavailableError):
            nb.get_news("APG1L", "2026-09-01", "2026-09-30")

    def test_an_html_bot_check_instead_of_json_is_vendor_unavailable(self):
        with mock.patch.object(nb.requests, "get", return_value=_response(text="<html>")), \
                pytest.raises(VendorUnavailableError, match="no JSON"):
            nb.get_news("APG1L", "2026-09-01", "2026-09-30")


class TestInsiderTransactions:
    def test_lists_managers_notifications_public_by_the_run_date(self):
        fake = FakeNasdaq(APRANGA)
        with _patched(fake):
            report = nb.get_insider_transactions("APG1L", "2026-08-31")
        assert report.startswith("# Insider Transactions for APG1L.VS (APB Apranga)")
        assert "# Point-in-time as of: 2026-08-31" in report
        assert report.count("### ") == 3  # two on 2026-08-27, one on 2026-06-10
        assert "Turnover" not in report
        assert "interim report" not in report

    def test_a_notification_after_the_run_date_is_not_shown(self):
        fake = FakeNasdaq(APRANGA)
        with _patched(fake):
            report = nb.get_insider_transactions("APG1L", "2026-08-26")
        assert report.count("### ") == 1  # only 2026-06-10

    def test_no_notifications_in_the_year_says_so(self):
        items = [_item(5, "2026-09-01 13:00:00", "Turnover of Apranga Group in August 2026"),
                 _item(4, "2025-01-01 13:00:00", "Older")]
        with _patched(FakeNasdaq(items)):
            report = nb.get_insider_transactions("APG1L", "2026-09-30")
        assert "No managers' transactions were notified by Apranga" in report

    @pytest.mark.parametrize(("headline", "category", "expected"), [
        ("Notification on manager's transactions", "Other information", True),
        ("AB Akola Group notification on transactions in the Company's securities by the "
         "person discharging managerial responsibilities", "Other information", True),
        ("Notification on the transactions concluded by manager in issuer's securities",
         MANAGERS, True),
        ("Notification on the transactions concluded by manager in issuer's securities",
         "Other information", True),
        ("Notification of transactions by a legal entity closely associated with a manager",
         "Other information", True),
        ("Turnover of Apranga Group in August 2026", "Other information", False),
        ("Changes in the management board", "Notification on material event", False),
    ])
    def test_recognizes_managers_notifications(self, headline, category, expected):
        announcement = {"headline": headline, "category": category}
        assert nb.is_managers_transaction(announcement) is expected


class TestParsing:
    def test_release_time_with_and_without_offset(self):
        assert nb._parse_time("2026-10-01 16:00:00 +0300") == datetime(2026, 10, 1, 13, tzinfo=UTC)
        assert nb._parse_time("2026-10-01 13:00:00") == datetime(2026, 10, 1, 13, tzinfo=UTC)
        assert nb._parse_time("yesterday") is None
        assert nb._parse_time(None) is None

    def test_body_text_reads_only_the_view_body(self):
        text = nb.body_text(BODY_HTML)
        assert text.startswith("Retail turnover (including VAT)")
        assert "\n\n\n" not in text
        assert "Nasdaq Company News" not in text
        assert nb.body_text("<html><body><p>No body here</p></body></html>") == ""


class TestRouting:
    def test_default_chain_serves_baltic_shares_from_nasdaq(self):
        with _patched(FakeNasdaq(APRANGA)):
            report = router.route_to_vendor("get_news", "APG1L", "2026-09-01", "2026-09-30")
        assert "Company announcements published on Nasdaq Baltic" in report

    def test_default_chain_passes_other_tickers_to_yahoo(self, monkeypatch):
        monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "yfinance",
                            lambda *a, **k: "YAHOO NEWS")
        fake = FakeNasdaq(APRANGA)
        with _patched(fake):
            assert router.route_to_vendor("get_news", "NVDA", "2026-09-01", "2026-09-30") == "YAHOO NEWS"
        assert fake.calls == []

    def test_insider_chain_passes_other_tickers_to_yahoo(self, monkeypatch):
        monkeypatch.setitem(router.VENDOR_METHODS["get_insider_transactions"], "yfinance",
                            lambda *a, **k: "YAHOO INSIDERS")
        assert router.route_to_vendor("get_insider_transactions", "NVDA", None) == "YAHOO INSIDERS"

    def test_an_unreachable_nasdaq_falls_back_to_yahoo(self, monkeypatch):
        monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "yfinance",
                            lambda *a, **k: "YAHOO NEWS")

        def down(*a, **k):
            raise requests.ConnectionError("down")

        with mock.patch.object(nb.requests, "get", side_effect=down):
            assert router.route_to_vendor("get_news", "APG1L", "2026-09-01", "2026-09-30") == "YAHOO NEWS"
