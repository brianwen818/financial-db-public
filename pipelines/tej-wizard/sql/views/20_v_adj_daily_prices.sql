-- TEJ 調整後日行情 (waprcd1), one row per security per trading day.
--
-- SCOPE: close to, but not, the whole market. Every company in
-- raw/company-info/company-info.xlsx that ever listed on TSE, OTC or 創新板 and
-- was not delisted before 2000-09-04 (2,451 on 2026-09-29), plus the 0050 and
-- 0051 ETFs. Companies that only ever traded on 興櫃 are excluded by choice, but
-- a listed company keeps its earlier 興櫃 bars: filter market IN ('TSE','OTC',
-- 'TIB') for exchange-auction prices only. Delisted companies are retained, so
-- a cross-section must use the securities that have a bar on that date.
--
-- ADJUSTMENT: only open/high/low/close are back-adjusted. The quote and
-- reference columns sitting beside them -- bid, offer, next_ref_price,
-- next_limit_up, next_limit_down -- are RAW prices on the unadjusted scale.
-- 2330 on 2000-09-04 closes at 27.99 adjusted while next_ref_price reads
-- 133.50. Comparing or combining the two groups is always a bug.
--
-- Prices are in NTD. Volume and shares outstanding are in thousands, turnover
-- in thousands of NTD, market cap in millions -- the units TEJ delivers, kept
-- rather than rescaled so a number here matches the same number in the add-in.
CREATE OR REPLACE VIEW v_adj_daily_prices AS
SELECT
    date, stock_id, stock_name,
    open, high, low, close,
    volume_k_shares, turnover_k, transactions,
    return_pct, return_ln, turnover_pct,
    shares_outstanding_k, market_cap_m,
    market_cap_weight_pct, turnover_weight_pct,
    pe_tse, pe_tej, pbr_tse, pbr_tej, psr_tej,
    div_yield_tse, cash_div_yield,
    price_change, high_low_spread_pct,
    bid, offer, next_ref_price, next_limit_up, next_limit_down,
    limit_flag, attention_flag, disposition_flag, full_delivery_flag,
    market,
    source_file, ingested_at,
    -- Hive partition keys. These are what actually prune the scan, so a range
    -- query should constrain `year` as well as `date`. `month` arrives as
    -- VARCHAR ('08') from the directory name, so it is cast for arithmetic.
    CAST(year AS INTEGER)  AS year,
    CAST(month AS INTEGER) AS month
FROM read_parquet('{{DB_ROOT}}/processed/adj-daily-prices/year=*/month=*/*.parquet',
                  hive_partitioning = 1);

-- One row per security: the universe this database actually covers.
-- stock_name and market are taken at the security's most recent bar, because
-- both change over a 26-year history (several of these moved OTC -> TSE).
CREATE OR REPLACE VIEW v_securities AS
SELECT
    stock_id,
    last(stock_name ORDER BY date)  AS stock_name,
    last(market ORDER BY date)      AS market,
    min(date)                       AS first_date,
    max(date)                       AS last_date,
    count(*)                        AS trading_days,
    max(date) = (SELECT max(date) FROM v_adj_daily_prices) AS is_current
FROM v_adj_daily_prices
GROUP BY stock_id;
