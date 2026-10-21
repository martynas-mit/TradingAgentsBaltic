"""What every Yahoo request shares: its retry, and how a failure or an empty answer is reported."""

import logging
import time

import yfinance as yf
from yfinance.exceptions import YFPricesMissingError, YFRateLimitError

from tradingagents.dataflows import proxy
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.net import vendor_reachable

logger = logging.getLogger(__name__)

YAHOO_HOST = "https://query2.finance.yahoo.com"


def raise_for_empty(symbol: str, canonical: str, what: str) -> None:
    """Report an empty Yahoo answer as an absence, or as an outage if it is one.

    An empty answer from a Yahoo that cannot be reached is not an answer about
    the symbol, so the host is probed before "this symbol has no {what}" is said.
    """
    if not vendor_reachable(YAHOO_HOST):
        raise VendorUnavailableError(f"Yahoo Finance is unreachable; no {what} was retrieved")
    raise NoMarketDataError(symbol, canonical, f"no {what}")


# yfinance answers some failed requests with an empty result, which would read as
# "no data" for a symbol nobody checked; raised, the failure is reported as one.
yf.config.debug.hide_exceptions = False


def _answered_empty(exc: Exception) -> bool:
    """Whether Yahoo answered that it has nothing: no such symbol (HTTP 404), or a
    price window with no prices in it.

    yfinance raises YFPricesMissingError for that answer and also for an answer
    that was an error (an error status, or Yahoo describing a failure), so only
    a chart with no prices, or Yahoo saying the data does not exist, counts.
    A missing time zone is not an answer: yfinance reports a failed lookup the same way.
    """
    if isinstance(exc, YFPricesMissingError):
        reason = exc.yahoo_reason
        return "status_code" not in (exc.debug_info or "") and (
            reason is None or reason.startswith("Data doesn't exist"))
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


def _use_proxy_if_blocked() -> None:
    """Point yfinance at the proxy while Yahoo is marked as blocking, else direct."""
    target = proxy.yfinance_proxy()
    if yf.config.network.proxy != target:
        yf.set_config(proxy=target)


def _switch_to_proxy() -> bool:
    """After a failed or throttled request: whether a proxy is available and not yet in use."""
    if not proxy.eligible(YAHOO_HOST) or yf.config.network.proxy:
        return False
    proxy.mark_blocked(YAHOO_HOST)
    _use_proxy_if_blocked()
    return True


def yf_retry(func, max_retries=3, base_delay=2.0):
    """Execute a yfinance call with exponential backoff on rate limits.

    yfinance raises YFRateLimitError on HTTP 429 responses but does not
    retry them internally, so this wrapper retries them. A rate limit that
    outlasts the retries, or any other exception, is raised as
    VendorUnavailableError: it failed in transit and says nothing about the
    symbol. Yahoo answering that it has nothing for the symbol returns None,
    an empty answer. ``func`` should build its own Ticker and make the request
    itself: a Ticker keeps a failed ``info`` fetch as done, so asking the same
    one again reads an empty profile.
    """
    _use_proxy_if_blocked()
    for attempt in range(max_retries + 1):
        try:
            return func()
        except YFRateLimitError as exc:
            if attempt == max_retries and _switch_to_proxy():
                return yf_retry(func, max_retries=0, base_delay=base_delay)
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise VendorUnavailableError(
                    f"Yahoo Finance rate limited after {max_retries} retries: {exc}"
                ) from exc
        except Exception as exc:
            if _answered_empty(exc):
                return None
            if _switch_to_proxy():
                return yf_retry(func, max_retries=0, base_delay=base_delay)
            raise VendorUnavailableError(f"Yahoo Finance request failed: {type(exc).__name__}") from exc
