"""ESEF annual statements from filings.xbrl.org, and the Baltic statement tools.

Responses are recorded shapes of filings.xbrl.org (October 2026): the entity's
filing list (JSON:API) and xBRL-JSON reports, cut to a few facts.
"""

from unittest import mock

import pytest
import requests

from tests.test_nasdaq_baltic import BODY_HTML, _item, _response
from tradingagents.dataflows import router
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import baltic, esef, nasdaq_baltic as nb

pytestmark = pytest.mark.unit

LEI = "529900XX7EU9F4GJTY86"
FY = "{y}-01-01T00:00:00/{n}-01-01T00:00:00"
END = "{n}-01-01T00:00:00"


def _fact(concept, period, value, unit="iso4217:EUR", **extra):
    dims = {"concept": concept, "entity": f"scheme:{LEI}", "period": period, "unit": unit, **extra}
    return {"value": str(value), "decimals": -3, "dimensions": dims}


def _report(year, profit, prior_profit, assets, prior_assets, extra=()):
    facts = [
        _fact("ifrs-full:RevenueFromContractsWithCustomers", FY.format(y=year, n=year + 1), 300_000_000 + year),
        _fact("ifrs-full:ProfitLoss", FY.format(y=year, n=year + 1), profit),
        _fact("ifrs-full:ProfitLoss", FY.format(y=year - 1, n=year), prior_profit),
        _fact("ifrs-full:BasicEarningsLossPerShare", FY.format(y=year, n=year + 1), 0.29,
              unit="iso4217:EUR/xbrli:shares"),
        _fact("ifrs-full:Assets", END.format(n=year + 1), assets),
        _fact("ifrs-full:Assets", END.format(n=year), prior_assets),
        _fact("ifrs-full:CashFlowsFromUsedInOperatingActivities", FY.format(y=year, n=year + 1), 38_988_000),
        # A half-year figure, a dimensioned (separate-company) fact and an extension: all skipped.
        _fact("ifrs-full:ProfitLoss", f"{year}-01-01T00:00:00/{year}-07-01T00:00:00", 1),
        _fact("ifrs-full:ProfitLoss", FY.format(y=year, n=year + 1), 2,
              **{"ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis": "ifrs-full:SeparateMember"}),
        _fact("apbapranga:StoreCount", FY.format(y=year, n=year + 1), 174),
        *extra,
    ]
    return {"documentInfo": {"documentType": "https://xbrl.org/2021/xbrl-json"},
            "facts": {f"f{i}": f for i, f in enumerate(facts)}}


def _filing(fid, period_end, added, lang="en"):
    return {"type": "filing", "id": fid, "attributes": {
        "period_end": period_end, "date_added": f"{added} 13:17:11.982019",
        "json_url": f"/{LEI}/{period_end}/ESEF/LT/0/apbapranga-{period_end}-{lang}.json"}}


FILINGS = [
    _filing("24419", "2025-12-31", "2026-04-07"),
    _filing("18471", "2024-12-31", "2025-05-07"),
    _filing("18472", "2024-12-31", "2025-05-07", "lt"),
    _filing("14554", "2023-12-31", "2024-05-01"),
]
REPORTS = {
    "2025-12-31-en": _report(2025, 16_216_000, 15_960_000, 171_528_000, 164_968_000),
    "2024-12-31-en": _report(2024, 15_960_000, 15_100_000, 164_968_000, 158_000_000),
    "2024-12-31-lt": _report(2024, 99, 99, 99, 99),
    "2023-12-31-en": _report(2023, 15_000_000, 14_000_000, 158_000_000, 150_000_000),
}

INTERIM = [
    _item(1466000, "2026-07-29 13:00:00", "Apranga Group interim report for 6 months of 2026",
          "Half-Yearly information", ("Apranga 6m 2026.pdf",)),
    _item(1462001, "2026-07-01 13:00:00", "Turnover of Apranga Group in June 2026"),
    _item(1450000, "2026-04-29 13:00:00", "Apranga Group interim report for 3 months 2026",
          "Interim information", ("Apranga 3m 2026.pdf",)),
    _item(1440000, "2025-10-29 13:00:00", "Apranga Group interim report for 9 months 2025",
          "Interim information"),
]


class FakeSources:
    def __init__(self, filings=FILINGS, reports=REPORTS, xbrl_up=True, nasdaq_items=INTERIM):
        self.filings, self.reports, self.xbrl_up = filings, reports, xbrl_up
        self.nasdaq_items = nasdaq_items
        self.xbrl_calls: list[str] = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        if url.startswith(esef.BASE_URL):
            self.xbrl_calls.append(url)
            if not self.xbrl_up:
                raise requests.ConnectionError("down")
            if url.endswith("/filings"):
                return _response(payload={"data": self.filings, "meta": {"count": len(self.filings)}})
            key = url.rsplit("apbapranga-", 1)[1].removesuffix(".json")
            return _response(payload=self.reports[key])
        if url == nb.API_URL:
            start = int(params["start"])
            return _response(payload={"results": {"item": self.nasdaq_items[start:start + 100]}})
        return _response(text=BODY_HTML)


@pytest.fixture(autouse=True)
def _fresh(tmp_path):
    nb.clear_cache()
    with run_config({"data_cache_dir": str(tmp_path)}):
        yield
    nb.clear_cache()


def _web(fake):
    return mock.patch("requests.get", side_effect=fake)


class TestEsef:
    def test_annual_income_statement_as_filed(self):
        with _web(FakeSources()):
            text = esef.statement(LEI, "income", "APG1L.VS", "2026-10-16")
        assert text.startswith("# Income Statement for APG1L.VS (annual, as filed in ESEF)")
        assert "| Line | FY to 2025-12-31 | FY to 2024-12-31 | FY to 2023-12-31 | FY to 2022-12-31 |" in text
        # FY2023 as restated in the FY2024 report (15,100), not as first filed (15,000).
        assert "| Net profit | 16,216 | 15,960 | 15,100 | 14,000 |" in text
        assert "| Basic EPS (EUR) | 0.29 | 0.29 | 0.29 |" in text
        assert "StoreCount" not in text and "| 0 |" not in text

    def test_year_end_instants_read_as_the_closing_day(self):
        with _web(FakeSources()):
            text = esef.statement(LEI, "balance", "APG1L.VS", "2026-10-16")
        assert "| Total assets | 171,528 | 164,968 | 158,000 | 150,000 |" in text

    def test_a_report_not_yet_filed_is_not_served(self):
        with _web(FakeSources()):
            text = esef.statement(LEI, "income", "APG1L.VS", "2026-04-06")  # FY2025 added 04-07
        assert "FY to 2025-12-31" not in text
        assert "| Net profit | 15,960 | 15,100 | 14,000 |" in text
        assert "added 2025-05-07" in text

    def test_a_restatement_is_not_served_before_it_was_filed(self):
        with _web(FakeSources()):
            text = esef.statement(LEI, "income", "APG1L.VS", "2025-01-01")
        assert "| Net profit | 15,000 | 14,000 |" in text  # FY2023 as first filed

    def test_english_report_wins_over_the_lithuanian_copy(self):
        with _web(FakeSources()):
            values, filings = esef.statement_values(LEI, "income", "2025-06-01")
        assert all(f["json_url"].endswith("-en.json") for f in filings)
        assert 99.0 not in values["ifrs-full:ProfitLoss"].values()

    def test_nothing_filed_by_the_date_is_no_data(self):
        with _web(FakeSources()), pytest.raises(NoMarketDataError, match="no ESEF annual report"):
            esef.statement(LEI, "income", "APG1L.VS", "2020-01-01")

    def test_reports_are_cached(self):
        fake = FakeSources()
        with _web(fake):
            esef.statement(LEI, "income", "APG1L.VS", "2026-10-16")
            calls = len(fake.xbrl_calls)
            esef.statement(LEI, "cashflow", "APG1L.VS", "2026-10-16")
        assert len(fake.xbrl_calls) == calls

    def test_unreachable_is_vendor_unavailable(self):
        with _web(FakeSources(xbrl_up=False)), pytest.raises(VendorUnavailableError):
            esef.statement(LEI, "income", "APG1L.VS", "2026-10-16")


class TestBalticStatements:
    def test_quarterly_income_statement_adds_the_latest_interim_reports(self):
        with _web(FakeSources()):
            text = baltic.get_income_statement("APG1L", "quarterly", "2026-10-16")
        assert "# Income Statement for APG1L.VS (APB Apranga) (annual, as filed in ESEF)" in text
        interim = text[text.index("## Latest interim reports"):]
        assert "### Apranga Group interim report for 6 months of 2026 (published 2026-07-29)" in interim
        assert "### Apranga Group interim report for 3 months 2026" in interim
        assert "9 months 2025" not in interim       # only the latest two
        assert "Turnover" not in interim            # not an interim report
        assert "EUR 34.6 million" in interim        # the announcement text

    def test_an_interim_report_after_the_run_date_is_not_served(self):
        with _web(FakeSources()):
            text = baltic.get_income_statement("APG1L", "quarterly", "2026-07-28")
        assert "6 months of 2026" not in text
        assert "3 months 2026" in text

    def test_annual_request_has_no_interim_section(self):
        with _web(FakeSources()):
            text = baltic.get_income_statement("APG1L", "annual", "2026-10-16")
        assert "Latest interim reports" not in text

    def test_balance_sheet_points_to_the_interim_summaries(self):
        with _web(FakeSources()):
            text = baltic.get_balance_sheet("APG1L", "quarterly", "2026-10-16")
        assert "| Total assets |" in text
        assert "interim report summaries are served with the income statement" in text

    def test_bank_gets_a_reading_note(self):
        with _web(FakeSources()):
            text = baltic.get_cashflow("ROE1L", "annual", "2026-10-16")
        assert "a bank's statements differ" in text

    def test_quarterly_without_any_annual_report_still_serves_interim(self):
        with _web(FakeSources(filings=[])):
            text = baltic.get_income_statement("APG1L", "quarterly", "2026-10-16")
        assert "<No ESEF annual report available" in text
        assert "## Latest interim reports" in text

    def test_annual_without_any_report_is_no_data(self):
        with _web(FakeSources(filings=[])), pytest.raises(NoMarketDataError):
            baltic.get_cashflow("APG1L", "annual", "2026-10-16")

    def test_other_tickers_pass_through_the_default_chain(self, monkeypatch):
        monkeypatch.setitem(router.VENDOR_METHODS["get_income_statement"], "sec_edgar",
                            lambda *a, **k: "SEC")
        fake = FakeSources()
        with _web(fake):
            assert router.route_to_vendor("get_income_statement", "AAPL", "quarterly", "2026-10-16") == "SEC"
        assert fake.xbrl_calls == []
