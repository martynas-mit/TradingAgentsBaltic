"""Baltic shares in the Sentiment analyst, and the Baltic macro aliases."""

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.dataflows.vendors import fred

pytestmark = pytest.mark.unit


class _LLM:
    def __init__(self):
        self.prompts = []

    def with_structured_output(self, *a, **k):
        raise NotImplementedError

    def invoke(self, messages):
        self.prompts.append(str(messages))
        return AIMessage(content="report")


def _run(monkeypatch, ticker):
    social = []
    monkeypatch.setattr(sentiment_analyst.get_news, "func", lambda *a: "NEWS BLOCK")
    monkeypatch.setattr(sentiment_analyst, "jev_screen", lambda ticker: None)
    for name in ("fetch_stocktwits_messages", "fetch_reddit_posts"):
        monkeypatch.setattr(sentiment_analyst, name,
                            lambda *a, _n=name, **k: social.append(_n) or "posts")
    llm = _LLM()
    node = sentiment_analyst.create_sentiment_analyst(llm)
    node({"company_of_interest": ticker, "trade_date": "2026-10-16", "messages": []})
    return social, llm.prompts[0]


def test_baltic_share_skips_the_social_feeds_and_says_why(monkeypatch):
    social, prompt = _run(monkeypatch, "APG1L")
    assert social == []
    assert prompt.count("Nasdaq Baltic shares are not discussed on StockTwits") == 2
    assert "NEWS BLOCK" in prompt


def test_other_tickers_still_read_the_social_feeds(monkeypatch):
    social, _ = _run(monkeypatch, "NVDA")
    assert social == ["fetch_stocktwits_messages", "fetch_reddit_posts"]


@pytest.mark.parametrize(("alias", "series"), [
    ("lithuania_hicp", "CP0000LTM086NEST"),
    ("latvia_hicp", "CP0000LVM086NEST"),
    ("estonia_hicp", "CP0000EEM086NEST"),
    ("lithuania_real_gdp", "CLVMNACSCAB1GQLT"),
    ("Latvia real GDP", "CLVMNACSCAB1GQLV"),
    ("estonia-real-gdp", "CLVMNACSCAB1GQEE"),
])
def test_baltic_macro_aliases_resolve_to_fred_series(alias, series):
    assert fred._resolve_series_id(alias) == series


def test_instrument_context_points_baltic_shares_to_euro_area_macro():
    from tradingagents.agents.context import build_instrument_context

    text = build_instrument_context("APG1L.VS")
    assert "listed on Nasdaq Vilnius" in text
    assert "'lithuania_hicp'" in text and "'ecb_deposit_rate'" in text
    assert "lithuania" not in build_instrument_context("NVDA")
    assert "'estonia_real_gdp'" in build_instrument_context("LHV1T")
