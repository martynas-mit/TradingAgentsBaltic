# Nasdaq Baltic support (fork notes)

This fork extends TradingAgents so Nasdaq Baltic shares (Vilnius, Riga, Tallinn)
are analyzed the way US shares are. Work lives on the `baltic` branch, based on
upstream v0.7.0; upstream files are changed as little as possible so the branch
rebases cleanly onto new releases.

Scope for now: shares only. Index funds and ETFs come later.

## What works today

| Area | Status |
| --- | --- |
| Tickers | `APG1L`, `APG1L.VS`, `ROE1L` all resolve to the Yahoo symbol (`.VS` Vilnius, `.RG` Riga, `.TL` Tallinn) |
| Prices and indicators | Yahoo, as for any other market (history from 2002 for both sample shares) |
| Benchmark for alpha | `.VS` against `^OMXVGI`, `.RG` against `^OMXRGI`, `.TL` against `^OMXTGI` |
| Instrument registry | `tradingagents/dataflows/baltic/instruments.toml`: issuer name in Nasdaq's announcement service, LEI, sector, former names |
| Company news | Not yet: Yahoo tags no articles to Baltic listings |
| Statements, insider trades, euro-area macro | Not yet |

```bash
tradingagents --ticker APG1L --date 2026-10-16 --analysts market,news --save --no-show
```

## Registry

Each share the Baltic vendors should know needs an `[[instrument]]` entry. Keep
your own copy outside the package with
`TRADINGAGENTS_BALTIC_REGISTRY=/path/to/instruments.toml` (or the
`baltic_registry_path` config key). The file documents every key.

## Sources checked (stage 1, October 2026)

| Source | Finding |
| --- | --- |
| Nasdaq announcement API `api.news.eu.nasdaq.com/news/query.action` | JSON; `company=<issuer>` filters by issuer; returns category (e.g. managers' transactions), EN/LT versions, PDF attachments; history from 2019 (Artea's includes the Šiaulių bankas years). `fromDate`/`toDate` are ignored, so pages are read newest first and cut by date. Served through Imperva: confirm a plain HTTP client gets JSON. |
| nasdaqbaltic.com market news | Same announcements, server-rendered HTML behind Incapsula; issuer filter takes the issuer name (`?issuer=Apranga`). |
| Yahoo Finance | Prices for `APG1L.VS`, `ROE1L.VS`, `LHV1T.TL`, `SAF1R.RG` and the OMX Vilnius, Riga, Tallinn and Baltic Benchmark GI indexes; no news articles for either sample share. |
| filings.xbrl.org | ESEF annual reports by LEI with xBRL-JSON (`json_url`) and the date each was added. |
| GDELT DOC 2.0 | No articles on Artea in 3 months; one request per 5 seconds. Low value. |

## Next stages

3. `nasdaq_baltic` vendor: company announcements for `get_news`, managers'
   transactions for `get_insider_transactions`.
4. Local media, cache, English summaries.
5. `baltic_filings`: ESEF annual and quarterly report figures, bank template.
6. `eu_macro`: ECB, Eurostat, Bank of Lithuania; Baltic sentiment without
   StockTwits and Reddit.
