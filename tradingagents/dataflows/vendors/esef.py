"""Annual statements as filed in ESEF, from filings.xbrl.org.

EU-listed companies file their annual report in ESEF: inline XBRL tagged with
the IFRS taxonomy. filings.xbrl.org collects those filings by the company's LEI
and serves each as xBRL-JSON, together with the date it was added. That date
is when the figures were public at the latest, so a run dated before it is not
served them: statements are point-in-time, as SEC EDGAR's are for US filers.

Each report carries the year and the year before; the newest report available
on the run's date wins for a year it covers, so a restatement replaces the
original only once it was filed. Only the consolidated, undimensioned facts are
read, and the company's own extension concepts are left out: they have no
common meaning across filers.

Interim (quarterly, half-year) statements are not filed in ESEF. The Baltic
vendor adds the text of the interim report announcements instead.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from tradingagents import __version__
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.files import replace_file

logger = logging.getLogger(__name__)

BASE_URL = "https://filings.xbrl.org"
_TIMEOUT = 60
_LIST_TTL_SECONDS = 24 * 3600
_YEARS = 4  # fiscal years shown
_HEADERS = {
    "User-Agent": f"TradingAgents/{__version__} (+https://github.com/TauricResearch/TradingAgents)",
    "Accept": "application/json",
}
_BASE_DIMENSIONS = {"concept", "entity", "period", "unit", "language"}

# (concept, label). Lines a filer did not tag are left out, so one list serves
# trading companies and banks alike; each section keeps the order of a statement.
INCOME_STATEMENT = [
    ("ifrs-full:Revenue", "Revenue"),
    ("ifrs-full:RevenueFromContractsWithCustomers", "Revenue from contracts with customers"),
    ("ifrs-full:InterestRevenueCalculatedUsingEffectiveInterestMethod", "Interest revenue (effective interest)"),
    ("ifrs-full:InterestExpense", "Interest expense"),
    ("ifrs-full:RevenueFromInterest", "Interest income, as tagged"),
    ("ifrs-full:FeeAndCommissionIncome", "Fee and commission income"),
    ("ifrs-full:FeeAndCommissionExpense", "Fee and commission expense"),
    ("ifrs-full:FeeAndCommissionIncomeExpense", "Net fee and commission income"),
    ("ifrs-full:InsuranceRevenue", "Insurance revenue"),
    ("ifrs-full:CostOfSales", "Cost of sales"),
    ("ifrs-full:GrossProfit", "Gross profit"),
    ("ifrs-full:SellingExpense", "Selling expense"),
    ("ifrs-full:GeneralAndAdministrativeExpense", "General and administrative expense"),
    ("ifrs-full:EmployeeBenefitsExpense", "Employee benefits expense"),
    ("ifrs-full:DepreciationAndAmortisationExpense", "Depreciation and amortisation"),
    ("ifrs-full:OtherGainsLosses", "Other gains (losses)"),
    ("ifrs-full:ProfitLossFromOperatingActivities", "Operating profit"),
    ("ifrs-full:FinanceIncome", "Finance income"),
    ("ifrs-full:FinanceCosts", "Finance costs"),
    ("ifrs-full:ProfitLossBeforeTax", "Profit before tax"),
    ("ifrs-full:IncomeTaxExpenseContinuingOperations", "Income tax expense"),
    ("ifrs-full:ProfitLoss", "Net profit"),
    ("ifrs-full:ProfitLossAttributableToOwnersOfParent", "Net profit attributable to owners"),
    ("ifrs-full:ComprehensiveIncome", "Total comprehensive income"),
    ("ifrs-full:BasicEarningsLossPerShare", "Basic EPS (EUR)"),
    ("ifrs-full:DilutedEarningsLossPerShare", "Diluted EPS (EUR)"),
]
BALANCE_SHEET = [
    ("ifrs-full:PropertyPlantAndEquipment", "Property, plant and equipment"),
    ("ifrs-full:RightofuseAssets", "Right-of-use assets"),
    ("ifrs-full:IntangibleAssetsAndGoodwill", "Intangible assets and goodwill"),
    ("ifrs-full:IntangibleAssetsOtherThanGoodwill", "Intangible assets other than goodwill"),
    ("ifrs-full:NoncurrentAssets", "Non-current assets"),
    ("ifrs-full:Inventories", "Inventories"),
    ("ifrs-full:InventoriesTotal", "Inventories"),
    ("ifrs-full:TradeAndOtherCurrentReceivables", "Trade and other receivables"),
    ("ifrs-full:LoansAndAdvancesToCustomers", "Loans and advances to customers"),
    ("ifrs-full:ConsumerLoans", "Loans to customers, as tagged"),
    ("ifrs-full:DebtSecurities", "Debt securities"),
    ("ifrs-full:FinancialAssetsAtFairValueThroughOtherComprehensiveIncome", "Financial assets at FVOCI"),
    ("ifrs-full:CashAndCashEquivalents", "Cash and cash equivalents"),
    ("ifrs-full:CurrentAssets", "Current assets"),
    ("ifrs-full:Assets", "Total assets"),
    ("ifrs-full:DepositsFromCustomers", "Deposits from customers"),
    ("ifrs-full:InsuranceContractsIssuedThatAreLiabilities", "Insurance contract liabilities"),
    ("ifrs-full:NoncurrentPortionOfNoncurrentBorrowings", "Non-current borrowings"),
    ("ifrs-full:CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings", "Current borrowings"),
    ("ifrs-full:NoncurrentLeaseLiabilities", "Non-current lease liabilities"),
    ("ifrs-full:CurrentLeaseLiabilities", "Current lease liabilities"),
    ("ifrs-full:TradeAndOtherCurrentPayables", "Trade and other payables"),
    ("ifrs-full:NoncurrentLiabilities", "Non-current liabilities"),
    ("ifrs-full:CurrentLiabilities", "Current liabilities"),
    ("ifrs-full:Liabilities", "Total liabilities"),
    ("ifrs-full:IssuedCapital", "Issued capital"),
    ("ifrs-full:TreasuryShares", "Treasury shares"),
    ("ifrs-full:RetainedEarnings", "Retained earnings"),
    ("ifrs-full:NoncontrollingInterests", "Non-controlling interests"),
    ("ifrs-full:Equity", "Total equity"),
]
CASH_FLOW = [
    ("ifrs-full:CashFlowsFromUsedInOperationsBeforeChangesInWorkingCapital", "Operating cash flow before working capital"),
    ("ifrs-full:InterestPaidClassifiedAsOperatingActivities", "Interest paid (operating)"),
    ("ifrs-full:IncomeTaxesPaidClassifiedAsOperatingActivities", "Income taxes paid"),
    ("ifrs-full:IncomeTaxesPaidRefundClassifiedAsOperatingActivities", "Income taxes paid"),
    ("ifrs-full:CashFlowsFromUsedInOperatingActivities", "Net cash from operating activities"),
    ("ifrs-full:PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets", "Capital expenditure"),
    ("ifrs-full:PurchaseOfPropertyPlantAndEquipment", "Purchase of property, plant and equipment"),
    ("ifrs-full:CashFlowsFromUsedInInvestingActivities", "Net cash from investing activities"),
    ("ifrs-full:DividendsPaidClassifiedAsFinancingActivities", "Dividends paid"),
    ("ifrs-full:PaymentsToAcquireOrRedeemEntitysShares", "Share buybacks"),
    ("ifrs-full:ProceedsFromBorrowingsClassifiedAsFinancingActivities", "Proceeds from borrowings"),
    ("ifrs-full:RepaymentsOfBorrowingsClassifiedAsFinancingActivities", "Repayments of borrowings"),
    ("ifrs-full:PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities", "Lease payments"),
    ("ifrs-full:CashFlowsFromUsedInFinancingActivities", "Net cash from financing activities"),
    ("ifrs-full:IncreaseDecreaseInCashAndCashEquivalents", "Net change in cash"),
]
STATEMENTS = {
    "income": ("Income Statement", INCOME_STATEMENT, "duration"),
    "balance": ("Balance Sheet", BALANCE_SHEET, "instant"),
    "cashflow": ("Cash Flow Statement", CASH_FLOW, "duration"),
}
_WANTED = {concept for _, lines, _ in STATEMENTS.values() for concept, _ in lines}


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def _get_json(url: str, params: dict | None = None) -> dict:
    try:
        response = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise VendorUnavailableError(
            f"filings.xbrl.org request failed ({status or type(exc).__name__})"
        ) from exc
    except ValueError as exc:
        raise VendorUnavailableError("filings.xbrl.org returned no JSON") from exc


def _cache_dir() -> Path:
    return Path(get_config()["data_cache_dir"]) / "esef"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda temp: Path(temp).write_text(json.dumps(data), encoding="utf-8"))


def filings_for(lei: str) -> list[dict]:
    """The LEI's ESEF filings: period end, date added, xBRL-JSON path. Cached for a day."""
    path = _cache_dir() / f"{lei}-filings.json"
    if path.exists() and time.time() - path.stat().st_mtime < _LIST_TTL_SECONDS:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    payload = _get_json(f"{BASE_URL}/api/entities/{lei}/filings", {"page[size]": 100})
    filings = []
    for entry in payload.get("data") or []:
        attrs = entry.get("attributes") or {}
        if attrs.get("period_end") and attrs.get("date_added") and attrs.get("json_url"):
            filings.append({
                "id": str(entry.get("id")),
                "period_end": attrs["period_end"],
                "date_added": attrs["date_added"][:10],
                "json_url": attrs["json_url"],
            })
    _write_json(path, filings)
    return filings


def _reported_facts(filing: dict) -> list[dict]:
    """The filing's undimensioned facts for the concepts read here. Cached for good."""
    path = _cache_dir() / f"{filing['id']}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    document = _get_json(f"{BASE_URL}{filing['json_url']}")
    facts = []
    for fact in (document.get("facts") or {}).values():
        dims = fact.get("dimensions") or {}
        if dims.get("concept") not in _WANTED or set(dims) - _BASE_DIMENSIONS:
            continue
        facts.append({"concept": dims["concept"], "period": dims.get("period", ""),
                      "unit": dims.get("unit", ""), "value": fact.get("value")})
    _write_json(path, facts)
    return facts


# --------------------------------------------------------------------------- #
# Periods and values
# --------------------------------------------------------------------------- #

def _day(stamp: str) -> date:
    """An XBRL date-time as the day it closes: midnight belongs to the day before."""
    moment = datetime.fromisoformat(stamp)
    if moment.time() == datetime.min.time() and "T" in stamp:
        moment -= timedelta(days=1)
    return moment.date()


def _period(period: str) -> tuple[str, date] | None:
    """("instant" | "duration", closing day) for a fiscal-year fact, else None."""
    if "/" in period:
        start, end = period.split("/", 1)
        start_day = datetime.fromisoformat(start).date()
        end_day = _day(end)
        if 340 <= (end_day - start_day).days <= 390:
            return "duration", end_day
        return None
    return "instant", _day(period)


def available_filings(lei: str, as_of_date: str) -> list[dict]:
    """The newest filing per fiscal year added by ``as_of_date``, English first, newest first."""
    best: dict[str, dict] = {}
    for filing in filings_for(lei):
        if filing["date_added"] > as_of_date:
            continue
        current = best.get(filing["period_end"])
        rank = (filing["date_added"], filing["json_url"].endswith("-en.json"))
        if current is None or rank > (current["date_added"], current["json_url"].endswith("-en.json")):
            best[filing["period_end"]] = filing
    return sorted(best.values(), key=lambda f: f["period_end"], reverse=True)


def statement_values(lei: str, kind: str, as_of_date: str) -> tuple[dict, list[dict]]:
    """``{concept: {year_end: value}}`` for one statement, and the filings read.

    The newest filing that reports a year supplies its value, so a figure the
    next report restated reads as restated once that report was public.
    """
    _, lines, shape = STATEMENTS[kind]
    concepts = {concept for concept, _ in lines}
    filings = available_filings(lei, as_of_date)[:_YEARS]
    values: dict[str, dict[date, float]] = {}
    for filing in filings:  # newest first: an earlier filing never overwrites
        for fact in _reported_facts(filing):
            if fact["concept"] not in concepts or fact["value"] is None:
                continue
            if not fact["unit"].startswith("iso4217:EUR"):
                continue  # the tables are in EUR; EPS units are EUR per share
            period = _period(fact["period"])
            if period is None or period[0] != shape:
                continue
            try:
                number = float(fact["value"])
            except (TypeError, ValueError):
                continue
            values.setdefault(fact["concept"], {}).setdefault(period[1], number)
    return values, filings


def _format_value(concept: str, value: float) -> str:
    if "EarningsLossPerShare" in concept:
        return f"{value:.2f}"
    return f"{value / 1000:,.0f}"


def statement(lei: str, kind: str, label: str, as_of_date: str | None = None) -> str:
    """One statement as a table, fiscal years as columns, newest first (EUR thousands)."""
    as_of_date = as_of_date or get_current_date()
    title, lines, _ = STATEMENTS[kind]
    values, filings = statement_values(lei, kind, as_of_date)
    if not values:
        raise NoMarketDataError(label, label, f"no ESEF annual report filed by {as_of_date}")
    years = sorted({day for by_year in values.values() for day in by_year}, reverse=True)[:_YEARS]

    header = "| Line | " + " | ".join(f"FY to {y:%Y-%m-%d}" for y in years) + " |"
    rule = "| --- |" + " --- |" * len(years)
    rows = []
    seen_labels = set()
    for concept, line_label in lines:
        by_year = values.get(concept)
        if not by_year or line_label in seen_labels:
            continue
        seen_labels.add(line_label)
        cells = [_format_value(concept, by_year[y]) if y in by_year else "" for y in years]
        rows.append(f"| {line_label} | " + " | ".join(cells) + " |")

    sources = ", ".join(f"FY {f['period_end'][:4]} report added {f['date_added']}" for f in filings)
    return (
        f"# {title} for {label} (annual, as filed in ESEF)\n"
        f"# Point-in-time as of: {as_of_date}\n"
        f"# Source: filings.xbrl.org ({sources}). EUR thousands unless marked; "
        f"consolidated IFRS figures as tagged by the company.\n\n"
        + "\n".join([header, rule, *rows]) + "\n"
    )
