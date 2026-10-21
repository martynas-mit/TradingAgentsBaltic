"""Proxy fallback when a source blocks this machine (dataflows/proxy.py)."""

from unittest import mock

import pytest
import requests
import yfinance as yf

from tests.test_nasdaq_baltic import APRANGA
from tradingagents.dataflows import proxy
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors import nasdaq_baltic as nb
from tradingagents.dataflows.vendors.yahoo import common

pytestmark = pytest.mark.unit

PROXY = "http://user-rotate:secret@p.webshare.io:80"
NASDAQ = "https://api.news.eu.nasdaq.com/news/query.action"


def _resp(status=200, text='{"results": {"item": []}}', ctype="application/json"):
    response = requests.Response()
    response.status_code = status
    response._content = text.encode()
    response.headers["Content-Type"] = ctype
    return response


class Web:
    """Answers direct requests with ``direct`` and proxied ones with ``proxied``."""

    def __init__(self, direct, proxied=None):
        self.direct, self.proxied = direct, proxied or _resp()
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, url, proxies=None, **kwargs):
        through = bool(proxies)
        if through:
            assert proxies == {"http": PROXY, "https": PROXY}
        self.calls.append((url, through))
        answer = self.proxied if through else self.direct
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def _fresh():
    proxy.reset()
    nb.clear_cache()
    yield
    proxy.reset()
    nb.clear_cache()
    yf.set_config(proxy=None)


def _cfg(**extra):
    return run_config({"proxy_url": PROXY, **extra})


def _get(web, url=NASDAQ, **kwargs):
    with mock.patch.object(proxy.requests, "get", side_effect=web):
        return proxy.get(url, **kwargs)


class TestFallback:
    def test_without_a_proxy_a_block_is_returned_as_it_came(self):
        web = Web(_resp(403))
        assert _get(web).status_code == 403
        assert web.calls == [(NASDAQ, False)]

    def test_a_blocking_status_is_retried_through_the_proxy(self):
        web = Web(_resp(403))
        with _cfg():
            assert _get(web).status_code == 200
        assert web.calls == [(NASDAQ, False), (NASDAQ, True)]
        assert proxy.usage() == {"api.news.eu.nasdaq.com": 1}

    def test_the_blocking_host_then_goes_straight_through_the_proxy(self):
        web = Web(_resp(429))
        with _cfg():
            _get(web)
            _get(web)
        assert web.calls[-1] == (NASDAQ, True)
        assert [c for c in web.calls if not c[1]] == [(NASDAQ, False)]

    def test_after_the_cooldown_direct_is_tried_again(self, monkeypatch):
        web = Web(_resp(403))
        with _cfg(proxy_cooldown_min=1):
            _get(web)
            now = proxy.time.time()
            monkeypatch.setattr(proxy.time, "time", lambda: now + 61)
            _get(web)
        assert web.calls[2] == (NASDAQ, False)

    def test_a_bot_check_page_instead_of_the_answer_is_a_block(self):
        page = _resp(200, "<html>Request unsuccessful. Incapsula incident ID</html>", "text/html")
        web = Web(page)
        with _cfg():
            _get(web, expect=lambda r: r.text.startswith("{"))
        assert web.calls[-1] == (NASDAQ, True)

    def test_a_bot_check_marker_alone_is_a_block(self):
        web = Web(_resp(200, "<html>_Incapsula_Resource</html>", "text/html"))
        with _cfg():
            _get(web, url="https://view.news.eu.nasdaq.com/view?id=x")
        assert web.calls[-1][1] is True

    def test_a_dropped_connection_is_retried_through_the_proxy(self):
        web = Web(requests.ConnectionError("reset"))
        with _cfg():
            assert _get(web).status_code == 200
        assert web.calls[-1] == (NASDAQ, True)

    def test_a_real_error_is_not_retried(self):
        web = Web(_resp(404, "not found", "text/plain"))
        with _cfg():
            assert _get(web).status_code == 404
        assert web.calls == [(NASDAQ, False)]

    @pytest.mark.parametrize("url", [
        "https://www.sec.gov/files/company_tickers.json",
        "https://api.anthropic.com/v1/messages",
        "https://api.stlouisfed.org/fred/series",
    ])
    def test_hosts_off_the_list_never_use_the_proxy(self, url):
        web = Web(_resp(403))
        with _cfg(proxy_mode="always"):
            assert _get(web, url=url).status_code == 403
        assert web.calls == [(url, False)]

    def test_subdomains_of_a_listed_host_qualify(self):
        assert not proxy.eligible("https://query2.finance.yahoo.com/v8")  # no proxy configured
        with _cfg():
            assert proxy.eligible("https://query2.finance.yahoo.com/v8")
            assert not proxy.eligible("https://notfinance.yahoo.com.evil.example/")


class TestModes:
    def test_always_goes_through_the_proxy_first(self):
        web = Web(_resp())
        with _cfg(proxy_mode="always"):
            _get(web)
        assert web.calls == [(NASDAQ, True)]

    def test_off_never_does(self):
        web = Web(_resp(403))
        with _cfg(proxy_mode="off"):
            _get(web)
        assert web.calls == [(NASDAQ, False)]

    def test_a_custom_host_list(self):
        with _cfg(proxy_hosts="example.com, news.google.com"):
            assert proxy.eligible("https://news.google.com/rss")
            assert not proxy.eligible(NASDAQ)

    def test_an_unknown_mode_is_an_error(self):
        with _cfg(proxy_mode="sometimes"), pytest.raises(ValueError, match="proxy_mode"):
            proxy.eligible(NASDAQ)

    def test_logs_never_show_the_password(self):
        assert proxy.masked(PROXY) == "http://p.webshare.io:80"


class TestVendors:
    def test_nasdaq_news_survives_a_block(self, tmp_path):
        body = '{"results": {"item": ' + __import__("json").dumps(APRANGA) + "}}"

        def web(url, proxies=None, **kwargs):
            if url == nb.API_URL:
                return _resp(200, body) if proxies else _resp(403, "denied", "text/html")
            return _resp(200, '<div id="view-body"><p>Text</p></div>', "text/html")

        with _cfg(data_cache_dir=str(tmp_path)), mock.patch("requests.get", side_effect=web):
            report = nb.get_news("APG1L", "2026-09-01", "2026-09-30")
        assert "Turnover of Apranga Group in August 2026" in report
        assert proxy.usage()["api.news.eu.nasdaq.com"] >= 1

    def test_yahoo_switches_yfinance_to_the_proxy_and_retries(self):
        calls = []

        def fetch():
            calls.append(yf.config.network.proxy)
            if len(calls) == 1:
                raise RuntimeError("blocked")
            return "prices"

        with _cfg():
            assert common.yf_retry(fetch) == "prices"
        assert calls == [None, PROXY]

    def test_yahoo_without_a_proxy_reports_the_failure(self):
        def fetch():
            raise RuntimeError("blocked")

        with pytest.raises(common.VendorUnavailableError):
            common.yf_retry(fetch)
        assert yf.config.network.proxy is None
