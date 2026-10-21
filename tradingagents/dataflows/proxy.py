"""A proxy to fall back on when a data source blocks this machine.

News and filing sites often refuse requests from cloud addresses (a Railway
container, say) that they serve from a home connection: an HTTP 403 or 429, a
dropped connection, or a bot-check page in place of the JSON or RSS asked for.
With a proxy configured, such a request is sent once more through it, and the
host then goes through the proxy for a while before a direct request is tried
again. The proxy is meant for a rotating residential endpoint such as
Webshare's (``http://<user>-rotate:<password>@p.webshare.io:80``).

Only the hosts listed in ``proxy_hosts`` ever use it. LLM providers, FRED and
SEC EDGAR are not on the list: their requests carry API keys or a declared
identity and go direct.

Config (``TRADINGAGENTS_*`` env or the config dict):
    proxy_url           the proxy URL; also read from WEBSHARE_PROXY_URL
    proxy_mode          off | fallback (default when a URL is set) | always
    proxy_hosts         hosts that may use the proxy (a host or its subdomains)
    proxy_cooldown_min  how long a blocking host goes through the proxy
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

import requests

from tradingagents.dataflows.config import get_config

logger = logging.getLogger(__name__)

DEFAULT_HOSTS = (
    "api.news.eu.nasdaq.com",
    "view.news.eu.nasdaq.com",
    "news.google.com",
    "filings.xbrl.org",
    "finance.yahoo.com",
)
BLOCK_STATUSES = frozenset({403, 407, 429, 503})
_BOT_CHECK = re.compile(r"incapsula|imperva|_Incapsula_Resource|captcha|access denied", re.IGNORECASE)

_lock = threading.Lock()
_blocked_until: dict[str, float] = {}
_usage: Counter[str] = Counter()


def _settings() -> tuple[str | None, str, tuple[str, ...], float]:
    config = get_config()
    url = config.get("proxy_url") or None
    mode = (config.get("proxy_mode") or ("fallback" if url else "off")).strip().lower()
    if mode not in {"off", "fallback", "always"}:
        raise ValueError(f"proxy_mode {mode!r} is not one of off, fallback, always")
    hosts = config.get("proxy_hosts") or DEFAULT_HOSTS
    if isinstance(hosts, str):
        hosts = tuple(h.strip().lower() for h in hosts.split(",") if h.strip())
    cooldown = float(config.get("proxy_cooldown_min") or 30) * 60
    return url, mode, tuple(hosts), cooldown


def _host_listed(host: str, hosts: tuple[str, ...]) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith(f".{h}") for h in hosts)


def masked(url: str | None) -> str:
    """The proxy URL without its password, for logs."""
    if not url:
        return ""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}:{parts.port}" if parts.hostname else "<proxy>"


def eligible(url: str) -> bool:
    """Whether a request to ``url`` may go through the proxy."""
    proxy_url, mode, hosts, _ = _settings()
    return bool(proxy_url) and mode != "off" and _host_listed(urlsplit(url).hostname, hosts)


def _key(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def via_proxy_now(url: str) -> bool:
    """Whether a request to ``url`` goes through the proxy without trying direct first."""
    if not eligible(url):
        return False
    _, mode, _, _ = _settings()
    if mode == "always":
        return True
    with _lock:
        return _blocked_until.get(_key(url), 0) > time.time()


def mark_blocked(url: str) -> None:
    """Send ``url``'s host through the proxy for the cooldown."""
    _, _, _, cooldown = _settings()
    with _lock:
        _blocked_until[_key(url)] = time.time() + cooldown
    logger.warning("%s blocks direct requests; using the proxy for %.0f min", _key(url), cooldown / 60)


def is_block(response: requests.Response, expect: Callable[[requests.Response], bool] | None) -> bool:
    """Whether a response is a block: a blocking status, or a bot-check page instead of the answer."""
    if response.status_code in BLOCK_STATUSES:
        return True
    if response.status_code >= 400:
        return False  # a real error (404, 500) is not cured by another address
    if expect is not None and not expect(response):
        return True
    headers = response.headers if isinstance(getattr(response, "headers", None), Mapping) else {}
    head = (response.text or "")[:2000] if "html" in str(headers.get("Content-Type", "")) else ""
    return bool(head and _BOT_CHECK.search(head))


def _send(url: str, proxied: bool, **kwargs) -> requests.Response:
    if proxied:
        proxy_url, _, _, _ = _settings()
        kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        with _lock:
            _usage[_key(url)] += 1
    return requests.get(url, **kwargs)


def get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 20,
    expect: Callable[[requests.Response], bool] | None = None,
) -> requests.Response:
    """``requests.get`` that retries a blocked request once through the proxy.

    ``expect`` tells an answer from a bot-check page (for instance, the body is
    JSON). Returns the response; the caller still checks its status. Raises the
    direct request's error when there is no proxy to fall back on.
    """
    kwargs = {"params": params, "headers": headers, "timeout": timeout}
    if via_proxy_now(url):
        return _send(url, True, **kwargs)
    try:
        response = _send(url, False, **kwargs)
    except (requests.ConnectionError, requests.Timeout) as exc:
        if not eligible(url):
            raise
        logger.info("Direct request to %s failed (%s); retrying through the proxy", _key(url), exc)
        mark_blocked(url)
        return _send(url, True, **kwargs)
    if is_block(response, expect) and eligible(url):
        mark_blocked(url)
        return _send(url, True, **kwargs)
    return response


def yfinance_proxy() -> str | None:
    """The proxy yfinance should use now, or None for direct requests."""
    return _settings()[0] if via_proxy_now("https://query2.finance.yahoo.com") else None


def usage() -> dict[str, int]:
    """Requests sent through the proxy in this process, by host."""
    with _lock:
        return dict(_usage)


def reset() -> None:
    """Forget blocked hosts and usage (tests, or a new run in the same process)."""
    with _lock:
        _blocked_until.clear()
        _usage.clear()
