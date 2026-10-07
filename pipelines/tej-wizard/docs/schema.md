# Schema reference

This database holds **two TEJ contracts**, and they are separate queries with
separate grains, units and update units. Do not blend them.

| | 調整後日行情 | 指數成分股 |
|---|---|---|
| TEJ 表 | `waprcd1` | `widxs` |
| Source of truth | `processing/schema.py` | `processing/index_schema.py` |
| 欄數 | 35 (A..AI) | 10 (A..J) |
| 一個檔 | 一檔證券的全歷史 | 一個指數的一個年度 |
| 一列 | 證券 × 交易日 | 指數 × 交易日 × 成分股 |
| Raw 佈局 | `raw/adj-daily-prices-ticker/<id>.xlsx` | `raw/daily-index-constituents/<INDEX>/<YYYY>.xlsx` |
| 主 view | `v_adj_daily_prices` | `v_index_constituents` |

Part 1 below is `waprcd1`; [Part 2](#part-2--指數成分股-widxs) is `widxs`.

# Part 1 · 調整後日行情 (waprcd1)

`src/tej_pipeline/processing/schema.py` is the executable source of truth.
This document explains the reasoning; that file enforces it.

## Measured facts

All measured against the 2,453 workbooks on 2026-09-29, after the company-info
expansion, not assumed.

| Property | Value |
|---|---|
| Workbooks | 2,453, one security each |
| Rows | 10,093,712 |
| Date range | 2000-09-04 → 2026-09-24 (6,425 trading days) |
| Rows per trading day | ~1,617 (median) |
| `(stock_id, date)` | **Unique** — verified by `verify.py` over all rows |
| Nulls in `stock_id` / `date` / `close` | none |
| Duplicate dates within a workbook | none |
| Header layout | identical across all 2,453 (35 columns, A..AI) |
| TEJ query | identical across all 2,453 (`waprcd1`, 33 fields, `DAY`) |
| Row counts | 4 (1209 益華) to 6,425 (the 2000-09-04 cohort) |
| Still trading | 1,992 of 2,453 |
| `market` domain | `TSE`, `OTC`, `REG`, `TIB`, `PSB` — securities move between them; see [Universe](#universe) |

The "no formulas" property was measured on the original 367 workbooks
(2026-09-05); the rest are built from one of them by `create_workbooks.py`,
which writes only static values.

## The query behind every workbook

Stored by the add-in in the A1 cell comment:

```
tSearchString = 2|||0|||waprcd1|||OPEN,HIGH,LOW,CLOSE,VOLUME,AMOUNT,ROI,
                TURNOVER,OUTSTANDING,MV,BID,OFFER,ROIB,MV%,AMT%,TRN_D,
                PER-TSE,PER-TEJ,PBR-TSE,PBR-TEJ,LIMIT,TEJ_PSR,DIV_YID,
                TEJ_CDIV,CLSCHG,HMLPCT,REFPRC,U_LIMIT,D_LIMIT,XATTN1,
                XATTN2,XSTAT1,PMKT,|||DAY
tDescending   = Y
```

`waprcd1` is TEJ's 調整後日行情 table. The 33 field codes plus the two identity
columns (`證券代碼`, `年月日`) make the 35-column sheet. `schema.py` holds this
list and `workbook_scan.py` compares each file against it, so a workbook built
from a different TEJ query is rejected rather than blended into the same
partitions.

## Column mapping

`證券代碼` splits into two columns; everything else is renamed 1:1.

| Excel | TEJ code | Raw header | Processed | Type |
|---|---|---|---|---|
| A | — | 證券代碼 | `stock_id`, `stock_name` | `VARCHAR` |
| B | — | 年月日 | `date` | `DATE` |
| C | OPEN | 開盤價(元) | `open` | `DOUBLE` |
| D | HIGH | 最高價(元) | `high` | `DOUBLE` |
| E | LOW | 最低價(元) | `low` | `DOUBLE` |
| F | CLOSE | 收盤價(元) | `close` | `DOUBLE` |
| G | VOLUME | 成交量(千股) | `volume_k_shares` | `DOUBLE` |
| H | AMOUNT | 成交值(千元) | `turnover_k` | `DOUBLE` |
| I | ROI | 報酬率％ | `return_pct` | `DOUBLE` |
| J | TURNOVER | 週轉率％ | `turnover_pct` | `DOUBLE` |
| K | OUTSTANDING | 流通在外股數(千股) | `shares_outstanding_k` | `DOUBLE` |
| L | MV | 市值(百萬元) | `market_cap_m` | `DOUBLE` |
| M | BID | 最後揭示買價 | `bid` | `DOUBLE` |
| N | OFFER | 最後揭示賣價 | `offer` | `DOUBLE` |
| O | ROIB | 報酬率-Ln | `return_ln` | `DOUBLE` |
| P | MV% | 市值比重％ | `market_cap_weight_pct` | `DOUBLE` |
| Q | AMT% | 成交值比重％ | `turnover_weight_pct` | `DOUBLE` |
| R | TRN_D | 成交筆數(筆) | `transactions` | `BIGINT` |
| S | PER-TSE | 本益比-TSE | `pe_tse` | `DOUBLE` |
| T | PER-TEJ | 本益比-TEJ | `pe_tej` | `DOUBLE` |
| U | PBR-TSE | 股價淨值比-TSE | `pbr_tse` | `DOUBLE` |
| V | PBR-TEJ | 股價淨值比-TEJ | `pbr_tej` | `DOUBLE` |
| W | LIMIT | 漲跌停 | `limit_flag` | `VARCHAR` |
| X | TEJ_PSR | 股價營收比-TEJ | `psr_tej` | `DOUBLE` |
| Y | DIV_YID | 股利殖利率-TSE | `div_yield_tse` | `DOUBLE` |
| Z | TEJ_CDIV | 現金股利率 | `cash_div_yield` | `DOUBLE` |
| AA | CLSCHG | 股價漲跌(元) | `price_change` | `DOUBLE` |
| AB | HMLPCT | 高低價差% | `high_low_spread_pct` | `DOUBLE` |
| AC | REFPRC | 次日開盤參考價 | `next_ref_price` | `DOUBLE` |
| AD | U_LIMIT | 次日漲停價 | `next_limit_up` | `DOUBLE` |
| AE | D_LIMIT | 次日跌停價 | `next_limit_down` | `DOUBLE` |
| AF | XATTN1 | 注意股票(A) | `attention_flag` | `VARCHAR` |
| AG | XATTN2 | 處置股票(D) | `disposition_flag` | `VARCHAR` |
| AH | XSTAT1 | 全額交割(Y) | `full_delivery_flag` | `VARCHAR` |
| AI | PMKT | 市場別 | `market` | `VARCHAR` |

Plus two provenance columns, mirroring the FinMind pipeline:
`source_file` (which workbook the row came from) and `ingested_at` (when the
partition was written — on a back-adjusted series this is how stale the
adjustment basis is). DuckDB also exposes the two Hive partition keys `year`
and `month`, bringing `v_adj_daily_prices` to 40 columns in total.

**Units are TEJ's, kept rather than rescaled** so a number here matches the same
number in the add-in: volume and shares outstanding in thousands, turnover in
thousands of NTD, market cap in millions of NTD.

## The trap: only OHLC are adjusted

`waprcd1` is the *adjusted* table, but only `open`/`high`/`low`/`close` are
back-adjusted. The quote and reference columns sitting beside them are raw
prices on the unadjusted scale:

| 2330, 2000-09-04 | value | scale |
|---|---|---|
| `close` | 27.99 | **back-adjusted** |
| `bid` / `offer` | 133.00 / 133.50 | raw |
| `next_ref_price` | 133.50 | raw |
| `next_limit_up` / `next_limit_down` | 142.50 / 124.50 | raw |

A 4.8× discrepancy on the same row. Comparing or combining the two groups is
always a bug; `schema.ADJUSTED_PRICE_COLUMNS` and
`schema.UNADJUSTED_PRICE_COLUMNS` name both sets, and `verify.py` pins the 2330
example so the distinction cannot silently disappear.

This also means **the raw columns are the stable ones**. Every dividend and
split rewrites the whole back-adjusted history, so `close` for 2000-09-04 drifts
for as long as TSMC pays a dividend, while `next_ref_price` for that bar never
moves. Golden-value tests use the raw columns for exactly this reason.

## Format quirks handled

Each is a real property of the source, and each has a test.

| Quirk | Evidence | Handling |
|---|---|---|
| Dates are Excel serials | `<v>36773</v>` is 2000-09-04 | `excel_serial_to_date`, epoch 1899-12-30 |
| The 1900 leap-year bug | Serials ≤ 60 are ambiguous | Raises; the data floor is 36773, far above it |
| `證券代碼` bundles id and name | `"2330 台積電"` in one cell | Split on the first space; name `NULL` if absent |
| `stock_id` is not numeric | `0050` must keep its leading zero | Always `VARCHAR` |
| **Empty cells are omitted entirely** | No `<c>` element at all for a blank | Cells keyed by their `r` reference, never positionally |
| Strings are shared | `t="s"` means `<v>` is an index | Resolved against sharedStrings.xml |
| A shared string can be split across runs | Formatted names arrive as several `<t>` | Runs joined, or names truncate |
| Rows are newest-first | `tDescending=Y` | Sorted ascending on write; order never assumed |
| Excel lock files | `~$0050.xlsx` while a workbook is open | Excluded from the workbook list, and the rebuild refuses to run |
| A workbook caught mid-save | One reported 8 TEJ blocks and no header row | `assert_raw_readable` fails loudly rather than ingesting a torn snapshot |

## Universe

2,453 securities: **2,451 companies from `raw/company-info/company-info.xlsx`**
plus the two ETFs 0050 (元大台灣50) and 0051 (元大中型100), which are not
companies and are not constituents of anything.

The company list is written by `scripts/list_company_tickers.py`. company-info
is TEJ's company master — 3,555 companies on 2026-09-28 — and a company is kept
when it has ever listed on TSE, OTC or 創新板 and was not delisted before
2000-09-04, the first date of the template skeleton. Of the 2,455 companies
that rule selects, 2,451 have prices; 5513, 8702, 8706 and 8709 (all delisted
2001–2003) came back from TEJ with no prices at all. The 365 securities that
have ever been in TWN50 or TM100 (Part 2) are all included.

Coverage of company-info by `上市別` (the company's **current** status, not its
history — 2311 日月光 reads `UNPUB` and has a full history):

| 上市別 | companies | with a workbook | coverage |
|---|---:|---:|---:|
| TSE | 1,064 | 1,064 | 100% |
| OTC | 893 | 893 | 100% |
| TIB | 31 | 31 | 100% |
| UNPUB | 981 | 414 | 42.2% |
| PUB | 85 | 47 | 55.3% |
| ROTC | 364 | 2 | 0.5% |
| GISA | 137 | 0 | 0% |
| **all** | **3,555** | **2,451** | **68.9%** |

What is deliberately **not** covered: companies that only ever traded on 興櫃
(negotiated prices, excluded by choice), companies delisted before 2000-09-04,
never-listed companies, and 創櫃板 (GISA). So this is close to every listed
company since 2000, but it is still not the whole market.

The original 120 workbooks were downloaded by hand; 246 more were built by
`create_workbooks.py` on 2026-09-05 (index members) and 2,090 on 2026-09-28
(company-info); see
[architecture.md](architecture.md#building-a-workbook-that-does-not-exist-yet).

> **Point-in-time membership now comes from Part 2, not from the Feather.**
> `v_index_constituents` is daily, official index membership, starts 2002-07-05,
> is queryable in SQL, and stays current. The Feather stops at 2026-07-31, is a
> monthly review snapshot expanded across days, and describes the *ETF's*
> holdings rather than the *index*. Keep using it only for ISIN and for the
> ETF's own review weights. See
> [Universe and known gaps](#universe-and-known-gaps) for what the price layer
> does and does not cover.

The Feather is the historical provenance for the *original* 120 workbooks, and
still the only source of ISIN and of the ETF's own review snapshot weights.
Measured 2026-09-03:

| Property | Value |
|---|---|
| Rows / columns | 284,362 / 14 |
| Date range | 2003-06-30 → 2026-07-31 |
| Dates / constituent union | 5,685 / 119 |
| `(date, stock_id)` | Unique; 0 duplicates |
| Constituents per date | 49–51; always agrees with `n_holdings` |
| Reviews | 93 (`review_id`) |
| Daily sum of `snapshot_weight_pct` | 94.07%–100.22% |
| Workbook reconciliation | Feather union + `0050` = the original 120 xlsx, before the 2026-09-05 expansion to 367 |

Its columns are:

```
date, stock_id, stock_name, isin, snapshot_ym, review_id, review_regime,
effective_date, next_effective_date, n_holdings, snapshot_weight_pct,
snapshot_shares_k, snapshot_amount_k, snapshot_pct_of_shares_outstanding
```

Every row satisfies `effective_date <= date < next_effective_date`. The four
`snapshot_*` measures are constant for a `(review_id, stock_id)` and are carried
over the daily membership rows; they are **review snapshot values, not daily
floating index weights**. Their per-date sum is not guaranteed to equal exactly
100%; consumers that normalise them into portfolio weights must make that an
explicit modelling step. The DuckDB build does not expose this file as a view;
read it with Pandas/pyarrow when you need ISIN or the ETF's own weights.
**Do not use it for membership** — `v_index_constituents` is daily, official and
current, and this file stops at 2026-07-31.

Measured 2026-09-29:

| market | rows | securities |
|---|---|---|
| TSE | 5,316,090 | 1,333 |
| OTC | 4,059,832 | 1,411 |
| REG (興櫃) | 702,268 | 1,305 |
| TIB (創新板) | 14,357 | 37 |
| PSB (興櫃戰略新板) | 1,165 | 17 |

The securities column sums to far more than 2,453 because a security appears
under every market it has traded on, and many moved up over the history — 興櫃
to OTC to TSE is the usual path. **Excluding 興櫃-only companies does not remove
興櫃 rows**: a company that later listed keeps its 興櫃 history, so filter
`market IN ('TSE','OTC','TIB')` when only exchange-auction prices are wanted.
`v_securities` reports the code at each security's most recent bar (TSE 1,306,
OTC 1,109, TIB 31, REG 7). `PSB` arrived with the 2026-09-28 expansion; its
bars run 2021-07-26 → 2023-12-29.

**461 of the 2,453 have stopped trading** and keep their full history — mergers
(日月光/矽品 into ASE 2018, 晶電 into 富采 2021), delistings (華映 2019), holding
company conversions, and most recently 2867 三商壽, whose last bar is
2026-08-31. Delisted companies are therefore not dropped, unlike FinMind's
`v_adj_daily_prices`, where only 7 of 767 delisted securities have adjusted
prices at all.

That is the point of this database, and it is also why a backtest **must** take
its universe from the data as of each date: for an index strategy, point-in-time
membership from `v_index_constituents`; for a market-wide one, the securities
that actually have a bar that day. Putting all 2,453 securities into every
historical date introduces exactly the selection/look-ahead bias the retained
history was collected to avoid.

## Known data gaps

**Since the 2026-09-28 expansion `v_security_gaps` reports 67,707 sessions
across 118 securities, and they have not been vetted one by one.** The largest
look like the 6526 case below — companies off the market for years between two
listings: 8102 傑霖科技 4,474 (2005-08-31 → 2023-10-26), 6604 儒億 4,355,
3135 凌航 3,961, 2432 倚天酷碁-創 3,633, 8089 康全電訊 3,303. Treat that as a
likely explanation, not a verified one, and check before relying on a gap.

Before the expansion, the report was 1,714 sessions across just **six**
securities, and every one was verified as a real suspension rather than a fetch
failure:

| Security | Sessions | Window | What happened |
|---|---|---|---|
| 6526 達發 | 1,263 | 2017-04-25 → 2022-06-21 | Off-market between its 興櫃 delisting and its 2022 listing. FinMind independently has 1,461 rows over the identical span with **zero** rows inside the gap. |
| 2396 精碟 | 164 | 2008-08-08 → 2009-04-07 | Suspension before delisting |
| 2101 南港 | 130 | 2000-10-06 → 2001-04-11 | Suspension |
| 6452 康友-KY | 95 | 2020-08-18 → 2020-12-31 | Trading halt |
| 2342 茂矽 | 56 | 2003-05-12 → 2003-07-29 | Suspension |
| 2448 晶電 | 6 | 2020-12-24 → 2020-12-31 | Halt before its merger into 富采 |

A **seventh** security worth knowing about does not appear here because it has
no gap, only an end: **2867 三商壽 stopped trading after 2026-08-31**, and
FinMind has nothing for it after 2026-08-20 either. It is what made the
refresher learn to tell "this security has stopped" apart from "this refresh
failed" — see [operations.md](operations.md).

---

# Part 2 · 指數成分股 (widxs)

`src/tej_pipeline/processing/index_schema.py` is the executable source of truth.

## Measured facts

All measured against the 48 workbooks on 2026-09-05, not assumed.

| Property | Value |
|---|---|
| Workbooks | 48 — TWN50 2002–2026 (25), TM100 2004–2026 (23) |
| Rows | 832,648 — TWN50 297,265, TM100 535,383 |
| Date range | TWN50 2002-07-05 → 2026-09-07, TM100 2004-11-30 → 2026-09-07 |
| Sessions | TWN50 5,955, TM100 5,354 |
| `(index_id, date, stock_id)` | **Unique** — 832,648 distinct over 832,648 rows |
| Dates served by two workbooks | **none** — the year files partition the history exactly |
| Constituents per session | TWN50 **46–51**, TM100 **98–102** — *not* a fixed 50/100 |
| Daily sum of `prev_weight_pct` | 100.000 ± 0.001, except 2002 (93.69–98.33) |
| Securities in the union | 365 — TWN50 122, TM100 338, both 95 |
| Nulls in `stock_id` / `date` / `prev_weight_pct` | none |
| Header layout | identical across all 48 (10 columns, A..J) |
| TEJ query | identical across all 48 (`widxs`, 7 fields, `DAY`) |

## The query behind every workbook

```
tSearchString = 3|||0|||widxs|||IDXWE,F_FLOAT,CAPFAC,ZSTK_AMT,BASEN,
                PRE_CLS,MV%,|||DAY
tDescending   = Y
```

`widxs` is TEJ's 指數成分股 table. The 7 field codes plus the three identity
columns (`公司代碼`, `年月日`, `成份股`) make the 10-column sheet.
`workbook_scan.scan_index` compares each file against this list, so a workbook
built from a different TEJ query is rejected rather than blended in.

Note the leading `3`, against `waprcd1`'s `2`. That query type is what makes one
seeded row expand into a whole constituent set — see [the refresh
mechanism](architecture.md#the-refresh-mechanism).

## Column mapping

`公司代碼` and `成份股` each split into two columns; everything else is 1:1.

| Excel | TEJ code | Raw header | Processed | Type |
|---|---|---|---|---|
| A | — | 公司代碼 | `index_id`, `index_name` | `VARCHAR` |
| B | — | 年月日 | `date` | `DATE` |
| C | — | 成份股 | `stock_id`, `stock_name` | `VARCHAR` |
| D | `IDXWE` | 指數因子 | `index_factor` | `DOUBLE` |
| E | `F_FLOAT` | 公眾流通係數 | `free_float_factor` | `DOUBLE` |
| F | `CAPFAC` | 比重上限因子 | `cap_factor` | `DOUBLE` |
| G | `ZSTK_AMT` | 股數 | `shares` | `DOUBLE` |
| H | `BASEN` | 指數基值 | `index_base_value` | `DOUBLE` |
| I | `PRE_CLS` | 前日調整收盤價 | `prev_adj_close` | `DOUBLE` |
| J | `MV%` | 前日市值比重 | `prev_weight_pct` | `DOUBLE` |

Plus `source_file` (`"TWN50/2026.xlsx"` — the bare filename repeats across
indices, so it alone is not an identity) and `ingested_at`. 14 columns, and the
view adds `year`/`month` for 16.

## The trap: the date is the effective date, the data is the previous session's

A row dated **D** is the constituent set in force **for session D**, computed
from **D-1**'s close. So:

```
1101 台泥   date 2026-09-07   prev_adj_close 24.60   ← 2026-09-04's close
1101 台泥   date 2026-09-04   prev_adj_close 24.45   ← 2026-09-03's close
```

Two consequences:

1. **This dataset runs one session ahead of the price dataset.** Measured on
   Saturday 2026-09-05, both indices already carried 2026-09-07 (the following
   Monday) while `v_adj_daily_prices` stopped at 2026-09-04. Any "seed every
   trading date up to today" rule leaves the newest row permanently unfetched.
2. **Joining on `date` is correct; comparing within a row is not.**
   `v_index_constituents.date = v_adj_daily_prices.date` pairs a session with
   the list actually in force for it. But `prev_adj_close` on that row is the
   *previous* session's close, so it will not equal that row's `close`.

`prev_adj_close` **is** back-adjusted, so it is on the same scale as the price
view's `open/high/low/close` and **not** on the scale of the raw quote columns
beside them (`bid`, `next_ref_price`, …).

## The trap: units differ from the price table

`shares` is in **shares**. `v_adj_daily_prices.shares_outstanding_k` is in
**thousands**. Same quantity, 1,000 apart:

```
1101  2026-09-07  v_index_constituents.shares            7,523,181,742
1101  2026-09-04  v_adj_daily_prices.shares_outstanding_k    7,523,182
```

## The trap: the constituent count is not fixed

Measured per-session counts are **46–51** for TWN50 and **98–102** for TM100.
TWN50 carried only 46–49 names through 2002 and reached 51 on two sessions in
2010 and one in 2021; TM100 varies either side of 100 around reviews. A check
asserting exactly 50 or 100 fails on correct data — `v_thin_index_days`
compares each session against its own index's local median instead.

For the same reason the weight sum only closes to 100 from 2003 onward. In 2002
the published weights genuinely sum to as little as 93.69%, because the index
was still filling up to 50 names. `verify.py` excludes 2002 from that check
rather than loosening the band for the whole history.

## Format quirks

Identical to Part 1 — absent cells omitted from the XML, Excel serial dates,
shared strings, newest-first rows, `~$` lock files — plus one of its own:

| Quirk | Evidence | Handling |
|---|---|---|
| A seeded row TEJ never filled | Columns A and B set, C..J empty | Dropped by the reader; it is a prompt, not an observation, and would otherwise become a member row with a NULL `stock_id` |
| Non-year files in the raw directory | `26-test.xlsx`, left by a manual experiment and moved to `temp/test/` on 2026-09-05 | Only `^\d{4}\.xlsx$` is ingested; anything else is logged and skipped, because it is a copy of data already in a year file and would duplicate every session it covers |

## Universe and known gaps

The 365 securities here are the **membership authority** for this database, and
supersede the 0050 Feather for that purpose — daily rather than
monthly-snapshot-expanded, official index membership rather than ETF holdings,
starting 2002-07-05 rather than 2003-06-30, and current rather than stopping at
2026-07-31. The Feather is still the only source of ISIN and of the ETF's own
review snapshot weights.

**Every index member now has price history.** `v_index_universe` reports 0 rows
with `has_price_history = false`, and `verify.py` checks it. That check going
non-zero is the normal signal that a security has newly joined an index and
needs `create_workbooks.py --tickers <id>` run once.

`v_index_price_gaps` is down to **39 rows across six securities**, all of them
suspensions around a merger or holding-company conversion rather than missing
data: `5854 合庫` (2011-11-21→12-01, 9 sessions), `3009 奇美電`
(2010-03-08→17, 8), `2822 農銀` (2006-04-19→28, 8), `2311 日月光`
(2018-04-18→27, 8), `2403 友尚` (2010-11-09→12, 4), `2854 寶來證`
(2011-09-21→22, 2).

Before the expansion this view held **330,972** rows: 246 TM100 members with no
workbook at all, plus `2331 精英`, `2363 矽統` and `2376 技嘉` — three TWN50
members from 2002-07-05 who left before the Feather's 2003-06-30 start, so the
old price universe never saw them. **0050 cross-sections between 2002-07 and
2003-06 used to be missing up to three constituents; they are not any more.**
