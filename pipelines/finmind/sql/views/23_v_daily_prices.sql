-- Unadjusted daily OHLCV, one Parquet file per trading day.
--
-- NOTE on pruning (measured, not assumed): filtering on `date` alone does NOT
-- prune files. The Hive keys are year and month, and DuckDB cannot infer them
-- from a `date` predicate, so it opens all 8,053 footers. Add a `year BETWEEN`
-- predicate, or use the daily_prices_between() macro, for ~4x less work.
--
-- Scope: the security-master universe only. The upstream whole-market endpoint
-- also returns ~40,900 warrants and TDRs per day; those are filtered out at
-- ingestion and are deliberately not in this database.
CREATE OR REPLACE VIEW v_daily_prices AS
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
FROM read_parquet('{{DB_ROOT}}/processed/daily-prices/year=*/month=*/*.parquet',
                  hive_partitioning = 1);

-- Prices joined to the security master. Convenient for exploration; prefer
-- v_daily_prices in anything performance-sensitive.
--
-- Joins v_securities_all, not v_stock_info_latest, so that delisted securities
-- keep their names instead of silently becoming NULL. `industry` is NULL for
-- delisted rows -- there is no upstream industry classification for them --
-- so filter on is_delisted before grouping by industry.
CREATE OR REPLACE VIEW v_daily_prices_enriched AS
SELECT p.*,
       s.stock_name,
       s.industry_category_norm AS industry,
       s.type,
       s.is_index,
       coalesce(s.is_delisted, FALSE) AS is_delisted,
       s.delisted_date
FROM v_daily_prices p
LEFT JOIN v_securities_all s USING (stock_id);
