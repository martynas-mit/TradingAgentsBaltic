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
| Company news | `baltic` vendor: the issuer's announcements on Nasdaq Baltic with their text (monthly turnover, results, dividends, AGM decisions), then press headlines from Google News in the market's language and English, without wire copies of the announcements; all dated by publication |
| Insider trades | `baltic` vendor: managers' transactions notified under MAR article 19 in the year to the run date; trade details are in each notice's text or PDF |
| Statements | `baltic` vendor: annual income statement, balance sheet and cash flow from the company's ESEF reports on filings.xbrl.org (by LEI), served only once filed, newest report winning for a restated year; a quarterly request adds the text of the two latest interim report announcements |
| Macro | FRED aliases `lithuania_hicp`, `latvia_hicp`, `estonia_hicp`, `lithuania_real_gdp`, `latvia_real_gdp`, `estonia_real_gdp` beside upstream's euro-area ones (`ecb_deposit_rate`, `euro_hicp`); the instrument context points agents at them for Baltic shares. Needs `FRED_API_KEY` |
| Sentiment | StockTwits and Reddit are skipped for Baltic shares and the prompt says why, so an empty feed is not read as neutral; the news block (announcements and press) carries the signal |

```bash
tradingagents --ticker APG1L --date 2026-10-16 --analysts market,news --save --no-show
```

Statements default to `baltic,sec_edgar,yfinance`: Baltic shares from ESEF,
US filers from SEC EDGAR, the rest from Yahoo. ESEF files only annual reports;
quarterly figures come as the company's interim report summaries.

The default news chain is `baltic,yfinance`: a share in the registry is served
from Nasdaq and Google News (either may be down; the other is still served); any
other ticker falls through to Yahoo as before. `nasdaq_baltic` alone serves the
announcements without the press. Local headlines must carry one of the
registry's `press_names`, since a company name can also be a common word. Announcement texts are cached under
`<data_cache_dir>/nasdaq_baltic/`.

Live check (reaches the network):

```bash
pytest -m integration tests/test_nasdaq_baltic_live.py -s
```

## Registry

Each share the Baltic vendors should know needs an `[[instrument]]` entry. Keep
your own copy outside the package with
`TRADINGAGENTS_BALTIC_REGISTRY=/path/to/instruments.toml` (or the
`baltic_registry_path` config key). The file documents every key.

## Sources checked (stage 1, October 2026)

| Source | Finding |
| --- | --- |
| Nasdaq announcement API `api.news.eu.nasdaq.com/news/query.action` | JSON; `company=<issuer>` filters by issuer; returns category (e.g. managers' transactions), EN/LT versions, PDF attachments; history from 2019 (Artea's includes the Šiaulių bankas years). `fromDate`/`toDate` are ignored, so pages are read newest first and cut by date. Served through Imperva; a plain HTTP client (curl, requests) gets JSON. Releases are in UTC (`+0000`) unless `timeZone` is set. Each announcement's text is in `#view-body` on its `messageUrl` page. |
| nasdaqbaltic.com market news | Same announcements, server-rendered HTML behind Incapsula; issuer filter takes the issuer name (`?issuer=Apranga`). |
| Yahoo Finance | Prices for `APG1L.VS`, `ROE1L.VS`, `LHV1T.TL`, `SAF1R.RG` and the OMX Vilnius, Riga, Tallinn and Baltic Benchmark GI indexes; no news articles for either sample share. |
| filings.xbrl.org | ESEF annual reports by LEI (`/api/entities/<LEI>/filings`) with xBRL-JSON (`json_url`) and the date each was added; English and Lithuanian copies, and re-filings, of the same year. Apranga from FY2021, Artea from FY2021 (as Šiaulių bankas). Instants are stamped at midnight after the day they close. |
| GDELT DOC 2.0 | No articles on Artea in 3 months; one request per 5 seconds. Low value. |
| Google News RSS `news.google.com/rss/search` | Keyless; `after:`/`before:` in the query bound the window, so past windows work; `hl=lt&gl=LT&ceid=LT:lt` for Lithuanian media (vz.lt, Delfi, 15min, LRT, tv3). Up to 100 items; titles end with ` - <source>`. |

## Not covered yet

- Index funds and ETFs (e.g. AUM5 on Xetra): a mode without company news and statements.
- Trade details inside managers' transaction PDFs; full interim statements (PDF).
- Unemployment and retail-trade series for the Baltic states (no FRED series verified yet).
