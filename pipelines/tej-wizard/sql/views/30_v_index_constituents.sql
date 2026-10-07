-- TEJ 指數成分股 (widxs), one row per index per trading day per constituent.
--
-- SCOPE: two indices -- TWN50 (台灣50) from 2002-07-05 and TM100 (台灣中型100)
-- from 2004-11-30. This is the point-in-time membership record: what the index
-- actually held on a given session, not a present-day list projected backwards.
-- Use it to build a historical universe; anything else will be survivorship-
-- biased.
--
-- THE DATE IS THE EFFECTIVE DATE AND THE DATA IS THE PREVIOUS SESSION'S.
-- prev_adj_close on a 2026-09-07 row is 2026-09-04's close, so this dataset
-- runs one session AHEAD of v_adj_daily_prices: on a Friday evening it already
-- carries Monday's constituent list. Joining it to a price on the same `date`
-- is correct -- that is exactly the list in force for that session -- but
-- comparing prev_adj_close to close on the same row is not.
--
-- prev_adj_close IS back-adjusted (TEJ rewrites it on every ex-dividend), which
-- puts it on the same scale as v_adj_daily_prices' open/high/low/close and NOT
-- on the scale of the raw quote columns beside them.
--
-- UNITS: `shares` is in SHARES, not thousands -- 1101 reads 7,523,181,742 here
-- against shares_outstanding_k = 7,523,182 in the price view. The two are the
-- same quantity 1,000 apart. Weights and factors are percentages and ratios as
-- TEJ delivers them, unrescaled.
CREATE OR REPLACE VIEW v_index_constituents AS
SELECT
    date, index_id, index_name, stock_id, stock_name,
    prev_weight_pct,
    index_factor, free_float_factor, cap_factor,
    shares, index_base_value, prev_adj_close,
    source_file, ingested_at,
    -- Hive partition keys. These are what actually prune the scan, so a range
    -- query should constrain `year` as well as `date`; the
    -- index_constituents_between() macro does it for you.
    CAST(year AS INTEGER)  AS year,
    CAST(month AS INTEGER) AS month
FROM read_parquet('{{DB_ROOT}}/processed/index-constituents/year=*/month=*/*.parquet',
                  hive_partitioning = 1);

-- One row per (index, security): when it was in the index and for how long.
-- stock_name is taken at the most recent bar because names change over a
-- 24-year history.
CREATE OR REPLACE VIEW v_index_membership AS
SELECT
    index_id,
    stock_id,
    last(stock_name ORDER BY date) AS stock_name,
    min(date)                      AS first_date,
    max(date)                      AS last_date,
    count(*)                       AS days_in_index,
    last(prev_weight_pct ORDER BY date) AS latest_weight_pct,
    max(date) = (SELECT max(date) FROM v_index_constituents c
                 WHERE c.index_id = m.index_id) AS is_current
FROM v_index_constituents m
GROUP BY index_id, stock_id;

-- Every security either index has ever held, and whether this database holds
-- its adjusted price history.
--
-- has_price_history is the honest coverage answer: the price layer is built
-- one workbook per security, so a member with none cannot be priced from
-- v_adj_daily_prices and a backtest over the period it was in the index would
-- silently drop it.
CREATE OR REPLACE VIEW v_index_universe AS
SELECT
    u.stock_id,
    u.stock_name,
    u.in_twn50,
    u.in_tm100,
    u.first_date,
    u.last_date,
    s.stock_id IS NOT NULL AS has_price_history
FROM (
    SELECT
        stock_id,
        last(stock_name ORDER BY last_date) AS stock_name,
        bool_or(index_id = 'TWN50')         AS in_twn50,
        bool_or(index_id = 'TM100')         AS in_tm100,
        min(first_date)                     AS first_date,
        max(last_date)                      AS last_date
    FROM v_index_membership
    GROUP BY stock_id
) u
LEFT JOIN v_securities s USING (stock_id);
