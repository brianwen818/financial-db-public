# Schema reference

`src/finmind_pipeline/processing/schema.py` is the executable source of truth.
This document explains the reasoning; that file enforces it.

## Verified API behaviour

Measured against the live API on 2026-08-26 with a Sponsor token. These are
observations, not assumptions.

| Behaviour | Result |
|---|---|
| Quota | Sponsor, 6,000 req/hr (SponsorPro expired 2026-06-03) |
| Whole-market call (`data_id` omitted) | **Works** for both price datasets |
| `end_date` with `data_id` omitted | **Silently ignored** — only `start_date` is returned |
| Per-ticker full history | **No row cap.** `2330` → 8,053 rows from 1994-10-01 in one call |
| `TaiwanStockPrice`, one day | 43,727 rows / 43,727 securities |
| `TaiwanStockPriceAdj`, one day | 2,805 rows — a strict subset of the security master |
| `TaiwanStockInfo` | 4,308 rows / 3,137 distinct `stock_id` |
| Non-trading day | Empty `data` list, HTTP 200 — normal, not an error |
| Quota exhausted | HTTP 402 |

Consequence: the daily update is ~5 calls, but a backfill must go per ticker
(3,137 calls per dataset) because the market endpoint yields only one day each.

### Scope: what is deliberately excluded

A single day of `TaiwanStockPrice` returns 43,727 securities, but only 2,813 of
them are in the security master. The other 40,914 break down as:

| Shape | Count | What they are |
|---|---|---|
| `######` | 38,214 | Warrants (權證) |
| `#####U`, `#####T` | 2,628 | TDRs and beneficiary certificates |
| other suffixes | 72 | Misc. structured products |

Over half of them (23,130 of 43,727 rows) have zero volume on any given day.
They are filtered out at ingestion. Including them would mean ~97M rows and
2–3 GB instead of ~12M rows and ~300 MB.

To include them later: remove `filter_to_universe` from `config/datasets.yml`
(applies going forward) and backfill history with ~8,100 whole-market day calls.

### Delisted securities — recovered, not excluded

`TaiwanStockInfo` is a current-state list, so a company that delisted before
this project's first snapshot (2026-08-26) never appears in it. Because the
backfill iterates the master, those securities were never fetched at all, and
the warehouse initially held survivors only — on a 2010 cross-section that hid
about 9% of the ordinary shares that actually traded.

They are recovered by sampling one whole-market day per month from 2004-02-11
(`scripts/discover_delisted.py`, 271 calls) and unioning that with
`TaiwanStockDelisting`. Neither source suffices alone:

| Source | Codes absent from the master |
|---|---|
| `TaiwanStockDelisting` only | 320 |
| Monthly sweep only | 296 |
| Both | 151 |
| **Total** | **767** |

The delisting dataset names barely a third of what the sweep finds, and misses
recent delistings too (`8115` 2023-10, `2730` 2024-03). The sweep in turn
misses securities that never traded on a sampled day (`1204` traded 145 days in
2005). 471 of the 767 have a name; the rest carry price history and nothing
else, and none of them have an industry classification.

Sampling density is settled empirically: one day per *year* (23 calls) finds
425 of the 441 ordinary shares the monthly sweep finds. Twelvefold more calls
bought 16 extra codes, only three of which lived under two months — so monthly
is where it stops, and a full day-by-day sweep (5,551 calls per dataset) is not
worth running.

Of the 767, **466 return price history**. The other 301 delisted before
2004-02-11, where FinMind's daily prices begin, and return an empty response
for every call — expected, not a failure.

The effect is visible in any historical cross-section. On 2010-09-15 the
warehouse now holds 1,585 non-index securities, 145 of which (9.1%) delisted
later and were absent before.

#### The adjusted series is still survivor-only

`TaiwanStockPriceAdj` does **not** cover delisted securities. Of the same 767
codes, 466 have unadjusted history but only **7** have adjusted history.
Verified against the live API one code at a time — `4103` 百略醫學, `5480`
統盟電子, `1787` 福盈科技 and others each return an empty adjusted response
while returning thousands of unadjusted rows. This is an upstream gap, not an
ingestion failure.

| View | Rows | Securities | Delisted covered |
|---|---|---|---|
| `v_daily_prices` | 11,725,682 | 3,612 | 466 of 767 |
| `v_adj_daily_prices` | 11,224,366 | 3,149 | 7 of 767 |

So the survivorship fix applies to the **unadjusted** series only. A backtest
that needs a bias-free universe has to work from `v_daily_prices` and handle
corporate actions itself; `v_adj_daily_prices` remains a survivors-only view of
the market whatever else changes.

**A floor remains.** Whole-market history starts 2004-02-11, and per-ticker
history for a delisted code starts there too (`2311` 日月光, listed since 1989,
returns nothing before it). Only 919 securities have pre-2004 data, all of them
survivors. Bias-free cross-sections are therefore possible from 2004-02-11
onward and structurally impossible before it.

## Datasets

### `daily_prices` / `adj_daily_prices`

Both share one shape. Raw keeps FinMind's names; processed is the query contract.

| Raw column | Raw type | Processed column | Processed type |
|---|---|---|---|
| `date` | string | `date` | `DATE` |
| `stock_id` | string | `stock_id` | `VARCHAR` |
| `open` | double | `open` | `DOUBLE` |
| `max` | double | **`high`** | `DOUBLE` |
| `min` | double | **`low`** | `DOUBLE` |
| `close` | double | `close` | `DOUBLE` |
| `spread` | double | `spread` | `DOUBLE` |
| `Trading_Volume` | int64 | `volume` | `BIGINT` |
| `Trading_money` | int64 | `turnover_value` | `BIGINT` |
| `Trading_turnover` | int64 | `transactions` | `BIGINT` |
| — | — | `source` | `VARCHAR` (`market` \| `ticker`) |
| — | — | `ingested_at` | `TIMESTAMP WITH TIME ZONE` |

Deduplication key is `(stock_id, date)`. Which source wins a conflict differs
by dataset (`dedupe_prefer` in `config/datasets.yml`):

- **`daily_prices`: `market` wins.** The whole-market pull is the settled
  end-of-day snapshot, whereas a ticker file may have been fetched mid-session.
- **`adj_daily_prices`: `ticker` wins.** A back-adjusted value is only
  meaningful on the basis it was fetched under. A ticker file is one consistent
  vintage of the whole history; a market-day file is a snapshot of whatever
  basis applied when that day was pulled. Market rows only fill the days the
  ticker file does not reach yet. See
  [changelog 2026-10-04](changelog/2026-10-04-adj-dedupe-ticker-first.md).

Layout: `year=YYYY/month=MM/YYYY-MM-DD.parquet`, one file per trading day.

### `tw_stock_info` — security master

| Column | Type | Notes |
|---|---|---|
| `snapshot_date` | `DATE` | **Nullable** — NULL for the 32 index rows |
| `stock_id` | `VARCHAR` | Not numeric; leading zeros significant |
| `stock_name` | `VARCHAR` | |
| `industry_category` | `VARCHAR` | Verbatim from upstream |
| `industry_category_norm` | `VARCHAR` | Normalised via `config/industry_alias.yml` |
| `type` | `VARCHAR` | `twse` / `tpex` / `emerging` |
| `is_index` | `BOOLEAN` | True for the 32 index pseudo-tickers |

A cumulative snapshot log, not a current-state table: 4,321 rows cover 3,147
securities across 263 scrape dates, because `industry_category` was renamed
upstream over time. No single column is a key. Use `v_stock_info_latest` for
current state.

**`snapshot_date` is when this content was first seen, not when it was last
crawled.** Upstream re-stamps most rows with the crawl date on every pull, so
rows are collapsed on content and the stamp kept as `min()`. Without that the
log would gain ~3,300 rows per run and a real reclassification would be
indistinguishable from a re-crawl. The per-crawl stamps stay in
`raw/tw-stock-info/`.

### `tw_industry` — industry chain

| Column | Type | Notes |
|---|---|---|
| `stock_id` | `VARCHAR` | |
| `industry` | `VARCHAR` | |
| `sub_industry` | `VARCHAR` | |
| `snapshot_date` | `DATE` | Last seen upstream — **not** an effective date |

**Many-to-many.** A stock carries 1–61 rows. The key is
`(stock_id, industry, sub_industry)` — *not* `stock_id`, and *not*
`(stock_id, snapshot_date)`. Joining this to prices without aggregating first
will multiply the row count. Use `v_industry_by_stock` for a 1:1 join.

**One row per key, enforced.** Upstream's `date` is a scrape stamp it rewrites
on every re-crawl, so snapshots are collapsed on the natural key with
`max(snapshot_date)`. There is no point-in-time industry history: filtering on
`snapshot_date` narrows the set, it does not rewind it. The raw stamps are kept
in `raw/tw-industry/`.

### `delisted_securities` — the universe the master cannot see

| Column | Type | Notes |
|---|---|---|
| `stock_id` | `VARCHAR` | |
| `stock_name` | `VARCHAR` | **Nullable** — only the 471 named by `TaiwanStockDelisting` |
| `delisted_date` | `DATE` | **Nullable** — likewise |
| `shape` | `VARCHAR` | `common` or `etf`; see `processing/universe.py` |
| `first_seen` | `DATE` | **Sampling observation, not a listing date** |
| `last_seen` | `DATE` | **Sampling observation, not a delisting date** |
| `source` | `VARCHAR` | `discovered` \| `delisting` \| `both` |

Key is `stock_id`. Membership is `(discovered ∪ delisting) − master`, restricted
to in-scope shapes.

`first_seen` / `last_seen` come from one sample per month, so the true listing
and delisting dates sit up to a month outside that window. Use `delisted_date`
when it is present and you need a real date.

Shape classification carries the whole scope decision: 108,406 warrants and
27,042 TDRs appear in the sweep and must not enter the universe. The rules and
their measured counts are in `src/finmind_pipeline/processing/universe.py`.

### `tw_delisting` — delisting log

Raw `TaiwanStockDelisting`, deduplicated to `(stock_id, stock_name,
delisted_date)`. 723 rows, 1995-09-23 to 2026-07-28. Feeds names and dates into
`delisted_securities`; not exposed as a view of its own.

`stock_name` sometimes embeds a quoted English name containing commas
(`錡電科,"Auto Server Co., Ltd."`), so anything exporting it must quote properly.

### `expanded_trading_dates` — trading calendar

| Column | Type | Notes |
|---|---|---|
| `date` | `DATE` | |
| `source` | `VARCHAR` | `finmind` (6,937) or `derived` (1,204) |

`TaiwanStockTradingDate` starts at 1999-01-05; price history reaches back to
1994-10-01. The 1,204 derived days fill that gap, unioned from every date
observed in the price data. Total 8,141 days, 1994-10-01 → 2026-12-31.

The calendar **extends into the future** (FinMind publishes the full year), so
`max(date)` is not "latest data" — use `v_last_trading_day`.

It is also **not perfectly accurate**. 2026-07-10 is listed as a trading day,
but the whole-market endpoint returns zero securities for it — a market closure
(typhoon days are the usual cause) that never made it back into the published
calendar. Such days are confirmed against the API once and recorded in
`processed/known-empty-dates/`, surfaced as `v_known_empty_days`, and excluded
from `v_missing_trading_days`. Without that, reconciliation would report the same
false gap and re-fetch it on every weekly run forever.

A day is only recorded as closed once it is **strictly older than the newest
date already held**. An empty response for today just means "not published yet",
and recording those would blind the pipeline to genuine gaps.

## Data-quality defects handled

Each of these is a real defect in the upstream data, and each has a test.

| Defect | Evidence | Handling |
|---|---|---|
| `date` is the literal string `"None"` | 32 rows in `TaiwanStockInfo` | Mapped to real NULL. `CAST(date AS DATE)` would throw |
| `stock_id` is not numeric | `00679B`, `TAIEX`, `TradingConsumersGoods` | Always `VARCHAR`; leading zeros preserved |
| `max` / `min` column names | Collide with SQL aggregates | Renamed to `high` / `low` in processed |
| `Trading_money` overflows int32 | Max observed 52,822,527,292 | All count columns `BIGINT` |
| Industry vocabulary drift | `創新板股票` vs upstream typo `創新版股票`; six `X`/`X類`/`X業` pairs; three ETF spellings | `industry_category_norm` via alias table |
| Same-day figures get revised | `0050` on 2026-08-20: volume 39,946,000 → 43,382,378 after settlement, prices unchanged | Daily job re-fetches a 3-day revision window |
| Pre-1999 includes Saturdays | 176 Saturdays among the 1,204 derived days | No weekday-only logic anywhere |
| Exchange calendar lists non-trading days | 2026-07-10: listed, but zero securities returned | Confirmed once against the API, recorded in `v_known_empty_days`, excluded from the gap report |
| `所有證券` looks like an index label but is not | Tags 36 numeric-coded warrants (`708785` 信驊中信9B購01) | Excluded from `index_categories`; only `Index` and `大盤` qualify |

## Views

| View | Purpose |
|---|---|
| `v_daily_prices` | Unadjusted OHLCV |
| `v_adj_daily_prices` | Back-adjusted OHLCV |
| `v_prices_combined` | Both side by side, with `adj_factor` |
| `v_daily_prices_enriched` | Prices joined to the security master |
| `v_stock_info_history` | Full snapshot log |
| `v_stock_info_latest` | Current state, one row per security |
| `v_securities` | Tradable securities (excludes the 32 indices) |
| `v_delisted_securities` | Securities that traded but are gone from the master |
| `v_securities_all` | Master + delisted — the correct thing to join prices to |
| `v_indices` | The 32 index series, which do carry real OHLCV |
| `v_industry` | Raw many-to-many mapping |
| `v_industry_by_stock` | One row per stock, industries as lists — safe to join 1:1 |
| `v_trading_calendar` | 8,141 trading days with `source` |
| `v_last_trading_day` | Latest day actually published |
| `v_daily_coverage` | Row count per trading day |
| `v_missing_trading_days` | Reconciliation check A |
| `v_thin_trading_days` | Reconciliation check B |
| `v_known_empty_days` | Calendar days confirmed closed against the API |
| `v_ingestion_runs` | Run history from the JSONL manifests |
