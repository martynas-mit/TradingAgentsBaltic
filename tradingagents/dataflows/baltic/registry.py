"""The Nasdaq Baltic instrument registry.

Yahoo knows a Baltic share by its symbol and little else: Nasdaq's
announcement service files it under the issuer's own name, ESEF filings under
its LEI, and the press under names the company no longer uses (Artea bankas was
Šiaulių bankas). The registry ties those together for one share, so each Baltic
vendor asks its source the way that source files the company.

The registry is a TOML file of ``[[instrument]]`` tables (see
``instruments.toml``, packaged next to this module). ``baltic_registry_path``
in the config, or ``TRADINGAGENTS_BALTIC_REGISTRY``, points at another one.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.symbols import normalize_symbol

# Yahoo's suffix for each Nasdaq Baltic exchange.
MARKETS = {".VS": "Vilnius", ".RG": "Riga", ".TL": "Tallinn"}
BALTIC_SUFFIXES = tuple(MARKETS)
SECTORS = frozenset({"general", "bank"})

_REQUIRED = ("ticker", "name", "issuer")
_OPTIONAL = ("lei", "sector", "aliases", "press_names")
_PACKAGED = "instruments.toml"


class RegistryError(ValueError):
    """The registry file is missing, unreadable, or has an invalid entry."""


@dataclass(frozen=True)
class BalticInstrument:
    """One Nasdaq Baltic share, as each Baltic source names it."""

    ticker: str
    name: str
    issuer: str
    lei: str | None = None
    sector: str = "general"
    aliases: tuple[str, ...] = ()
    press_names: tuple[str, ...] = ()

    @property
    def market(self) -> str:
        """The exchange: Vilnius, Riga or Tallinn."""
        return MARKETS[self.ticker[self.ticker.rindex("."):]]

    @property
    def search_names(self) -> tuple[str, ...]:
        """Every name to search news for, most specific first, without repeats."""
        seen: set[str] = set()
        names: list[str] = []
        for value in (self.name, self.issuer, *self.aliases):
            if value and value.casefold() not in seen:
                seen.add(value.casefold())
                names.append(value)
        return tuple(names)

    @property
    def press_markers(self) -> tuple[str, ...]:
        """Text a local headline must contain to be about the company (case-sensitive).

        A company named with a common word (Apranga is Lithuanian for clothing)
        needs markers such as the quoted form („Apranga); by default the name
        and the issuer name serve.
        """
        return self.press_names or (self.name, self.issuer)


def is_baltic(ticker: str) -> bool:
    """Whether ``ticker`` is listed on a Nasdaq Baltic exchange."""
    return isinstance(ticker, str) and normalize_symbol(ticker).endswith(BALTIC_SUFFIXES)


def _parse_entry(raw: object, where: str) -> BalticInstrument:
    if not isinstance(raw, dict):
        raise RegistryError(f"{where}: expected a table of keys")
    unknown = sorted(set(raw) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        raise RegistryError(f"{where}: unknown key(s) {unknown}")
    missing = [key for key in _REQUIRED if not isinstance(raw.get(key), str) or not raw[key].strip()]
    if missing:
        raise RegistryError(f"{where}: missing or empty {missing}")

    ticker = normalize_symbol(raw["ticker"])
    if not ticker.endswith(BALTIC_SUFFIXES):
        raise RegistryError(
            f"{where}: ticker {raw['ticker']!r} needs a Baltic suffix {list(BALTIC_SUFFIXES)}"
        )
    sector = raw.get("sector", "general")
    if sector not in SECTORS:
        raise RegistryError(f"{where}: sector {sector!r} is not one of {sorted(SECTORS)}")
    lei = raw.get("lei")
    if lei is not None and (not isinstance(lei, str) or len(lei) != 20 or not lei.isalnum()):
        raise RegistryError(f"{where}: lei {lei!r} is not a 20-character LEI")
    lists = {}
    for key in ("aliases", "press_names"):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise RegistryError(f"{where}: {key} must be a list of strings")
        lists[key] = tuple(v.strip() for v in value if v.strip())

    return BalticInstrument(
        ticker=ticker,
        name=raw["name"].strip(),
        issuer=raw["issuer"].strip(),
        lei=lei.upper() if lei else None,
        sector=sector,
        aliases=lists["aliases"],
        press_names=lists["press_names"],
    )


@lru_cache(maxsize=8)
def _load(path: str | None, mtime_ns: int | None) -> dict[str, BalticInstrument]:
    """Parse one registry file; cached per file and modification time."""
    try:
        if path is None:
            text = resources.files(__package__).joinpath(_PACKAGED).read_text(encoding="utf-8")
            source = _PACKAGED
        else:
            text = Path(path).read_text(encoding="utf-8")
            source = path
        data = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError(f"cannot read the Baltic registry {path or _PACKAGED}: {exc}") from exc

    entries = data.get("instrument", [])
    if not isinstance(entries, list):
        raise RegistryError(f"{source}: [[instrument]] must be an array of tables")
    registry: dict[str, BalticInstrument] = {}
    for index, raw in enumerate(entries):
        instrument = _parse_entry(raw, f"{source} instrument #{index + 1}")
        if instrument.ticker in registry:
            raise RegistryError(f"{source}: {instrument.ticker} is listed twice")
        registry[instrument.ticker] = instrument
    return registry


def load_registry(path: str | None = None) -> dict[str, BalticInstrument]:
    """The registry, keyed by Yahoo ticker.

    ``path`` overrides the configured ``baltic_registry_path``; with neither,
    the packaged registry is read. A changed file is read again.
    """
    path = path or get_config().get("baltic_registry_path")
    if path is None:
        return _load(None, None)
    try:
        mtime_ns = Path(path).stat().st_mtime_ns
    except OSError as exc:
        raise RegistryError(f"cannot read the Baltic registry {path}: {exc}") from exc
    return _load(str(path), mtime_ns)


def lookup(ticker: str, path: str | None = None) -> BalticInstrument | None:
    """The registry entry for ``ticker`` (``APG1L`` or ``APG1L.VS``), or None."""
    if not isinstance(ticker, str) or not ticker.strip():
        return None
    return load_registry(path).get(normalize_symbol(ticker))
