-- Back-adjusted daily OHLCV.
--
-- Every dividend or split rewrites this series' entire history for the affected
-- ticker -- 0050 opens at 4.43 in 2003 here versus 37.09 unadjusted. The whole
-- dataset is therefore re-fetched and rebuilt weekly rather than appended to.
-- `ingested_at` tells you how stale the adjustment basis is.
--
-- Coverage is narrower than the unadjusted series: the upstream adjusted
-- endpoint returns only ~2,805 securities per day and excludes warrants.
CREATE OR REPLACE VIEW v_adj_daily_prices AS
SELECT
    date, stock_id,
    open, high, low, close, spread,
    volume, turnover_value, transactions,
    source, ingested_at,
    -- Hive partition keys. These are what actually prune the scan, so a
    -- range query should constrain `year` as well as `date`. `month` arrives
    -- as VARCHAR ('08') from the directory name, so it is cast for arithmetic.
    CAST(year AS INTEGER)  AS year,
    CAST(month AS INTEGER) AS month
FROM read_parquet('{{DB_ROOT}}/processed/adj-daily-prices/year=*/month=*/*.parquet',
                  hive_partitioning = 1);

-- Unadjusted and adjusted side by side. The ratio is the cumulative
-- adjustment factor, useful for spotting a stale adjustment basis.
--
-- Measured 2026-09-03 over 11,726,045 rows: adj_* are NULL on the 499,202 rows
-- the narrower adjusted feed does not cover, and `adj_factor` is NULL on 784,674
-- -- those same rows plus the 285,472 where the unadjusted close is 0 (suspended
-- or untraded sessions, guarded against division by zero). Neither is an error.
--
-- year/month are exposed, and joined on, so that this view prunes like the other
-- two. Exposing them alone is not enough: the predicate would prune only the
-- unadjusted side and the join would still open every adjusted footer. Joining
-- on them lets DuckDB propagate a `year` filter to both sides.
--
-- Joining on year/month is redundant by construction -- both are the Hive keys
-- day_partition_path() derives from `date`, so equal dates always imply equal
-- year/month. Verified across the whole dataset: identical to a plain
-- (stock_id, date) join at 11,726,045 rows and 499,202 unmatched.
-- Measured for one quarter: 1.31s bare `date` predicate, 0.30s via
-- prices_combined_between(). Use the macro.
CREATE OR REPLACE VIEW v_prices_combined AS
SELECT
    r.date, r.stock_id,
    r.open  AS open,  r.high AS high, r.low AS low, r.close AS close,
    a.open  AS adj_open, a.high AS adj_high, a.low AS adj_low, a.close AS adj_close,
    r.volume, r.turnover_value, r.transactions,
    CASE WHEN r.close <> 0 THEN a.close / r.close END AS adj_factor,
    year, month
FROM v_daily_prices r
LEFT JOIN v_adj_daily_prices a USING (stock_id, date, year, month);
