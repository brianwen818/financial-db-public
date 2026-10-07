-- Example queries. Run against finmind.duckdb, ideally read-only:
--   duckdb -readonly "<repo>/db/finmind/finmind.duckdb"
--
-- NOT executed by build_db.py (files under queries/ are skipped), so nothing
-- here is smoke-tested and verify.py cannot catch it rotting. The date literals
-- and the row counts quoted in the comments below are a snapshot taken
-- 2026-09-03; treat them as illustration, not as assertions.

-- 1. Overall shape of the database
SELECT 'daily'  AS dataset, count(*) AS rows, count(DISTINCT stock_id) AS securities,
       min(date) AS from_date, max(date) AS to_date FROM v_daily_prices
UNION ALL
SELECT 'adj', count(*), count(DISTINCT stock_id), min(date), max(date) FROM v_adj_daily_prices;

-- 2. One security's recent history
SELECT date, open, high, low, close, volume
FROM v_daily_prices WHERE stock_id = '2330' AND date >= DATE '2026-01-01'
ORDER BY date DESC LIMIT 20;

-- 3. Adjusted vs unadjusted, showing the cumulative adjustment factor
SELECT date, close, adj_close, adj_factor
FROM v_prices_combined WHERE stock_id = '0050' AND date IN (
    DATE '2003-07-01', DATE '2015-07-01', DATE '2026-08-21');

-- 4. Most active securities on the latest trading day
SELECT p.stock_id, s.stock_name, p.close, p.volume, p.turnover_value
FROM v_daily_prices p
LEFT JOIN v_stock_info_latest s USING (stock_id)
WHERE p.date = (SELECT date FROM v_last_trading_day)
ORDER BY p.turnover_value DESC LIMIT 20;

-- 4b. A historical cross-section WITHOUT survivorship bias. v_securities_all
--     is the master plus the 767 delisted securities. This day has 1,585
--     non-index securities; joining v_stock_info_latest instead returns 1,440,
--     silently dropping 145 (9.1%) that delisted later.
SELECT p.stock_id, s.stock_name, s.is_delisted, s.delisted_date,
       p.close, p.turnover_value
FROM v_daily_prices p
JOIN v_securities_all s USING (stock_id)
WHERE p.date = DATE '2010-09-15' AND NOT s.is_index
ORDER BY p.turnover_value DESC LIMIT 20;

-- 5. Sector index performance (the index pseudo-tickers carry real OHLCV)
SELECT p.stock_id, s.stock_name, p.date, p.close, p.spread
FROM v_daily_prices p JOIN v_indices s USING (stock_id)
WHERE p.date = (SELECT date FROM v_last_trading_day)
ORDER BY p.spread DESC;

-- 6. Monthly close for one security. Constraining `year` is what prunes the
--    scan; a `date` predicate alone would open all 8,053 files.
SELECT year, month, last(close ORDER BY date) AS month_end_close
FROM v_daily_prices WHERE stock_id = '2330' AND year >= 2024
GROUP BY year, month ORDER BY year, month;

-- 6b. The same idea via the macro, which applies the year predicate for you.
SELECT stock_id, min(date) AS from_date, max(date) AS to_date, count(*) AS bars
FROM daily_prices_between('2024-01-01', '2024-03-31')
WHERE stock_id IN ('2330', '0050')
GROUP BY stock_id;

-- 7. Data health
SELECT (SELECT count(*) FROM v_missing_trading_days) AS missing_days,
       (SELECT count(*) FROM v_thin_trading_days)    AS thin_days,
       (SELECT count(*) FROM v_stock_info_history WHERE snapshot_date IS NULL) AS null_snapshot_dates;

-- 8. Recent pipeline runs
SELECT job, status, started_at, duration_seconds, error_count
FROM v_ingestion_runs ORDER BY started_at DESC LIMIT 15;
