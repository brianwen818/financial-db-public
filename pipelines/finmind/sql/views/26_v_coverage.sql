-- Reconciliation support: which trading days are missing, and which days look
-- suspiciously thin (a half-written or partially-served fetch).
--
-- Bounded by the observed data range rather than by today, so days that simply
-- have not been published yet are never reported as gaps.
-- Latest day actually published in the price data. Use this rather than
-- max(date) on v_trading_calendar, which runs to the end of the year.
CREATE OR REPLACE VIEW v_last_trading_day AS
SELECT max(date) AS date FROM v_daily_prices;

CREATE OR REPLACE VIEW v_daily_coverage AS
SELECT
    date,
    count(*)                          AS row_count,
    count(DISTINCT stock_id)          AS securities,
    max(ingested_at)                  AS last_ingested
FROM v_daily_prices
GROUP BY date;

-- Dates the published calendar lists but on which nothing actually traded,
-- confirmed against the API. 2026-07-10 is the known example: a market closure
-- that never made it back into TaiwanStockTradingDate. Recording them keeps the
-- gap report meaningful instead of permanently noisy.
CREATE OR REPLACE VIEW v_known_empty_days AS
SELECT date, confirmed_at
FROM read_parquet('{{DB_ROOT}}/processed/known-empty-dates/known_empty_dates.parquet');

CREATE OR REPLACE VIEW v_missing_trading_days AS
SELECT c.date
FROM v_trading_calendar c
LEFT JOIN v_daily_coverage d USING (date)
WHERE d.date IS NULL
  AND c.date BETWEEN (SELECT min(date) FROM v_daily_prices)
                 AND (SELECT max(date) FROM v_daily_prices)
  -- NOT EXISTS, not NOT IN: a single NULL date in the known-empty set would
  -- make NOT IN evaluate to UNKNOWN for every row and silently empty this
  -- view -- a gap report that fails quiet is worse than no gap report.
  AND NOT EXISTS (SELECT 1 FROM v_known_empty_days k WHERE k.date = c.date)
ORDER BY c.date;

-- A day whose row count is far below its neighbours almost always means a
-- partial write rather than a genuinely quiet session.
--
-- `neighbour_median` is a slight misnomer: the 11-row window includes the row
-- being tested, so a thin day drags its own median down and makes itself look
-- less anomalous. That biases towards false negatives, which is the safe
-- direction for a repair trigger, so it is left alone deliberately -- 2026-08-26
-- slipped through at ratio 0.5008. Do not tighten the 0.5 threshold without
-- also excluding the tested row, or ordinary quiet sessions start getting
-- refetched every week.
CREATE OR REPLACE VIEW v_thin_trading_days AS
SELECT date, row_count, neighbour_median,
       round(row_count::DOUBLE / nullif(neighbour_median, 0), 3) AS ratio
FROM (
    SELECT date, row_count,
           median(row_count) OVER (
               ORDER BY date ROWS BETWEEN 5 PRECEDING AND 5 FOLLOWING
           ) AS neighbour_median
    FROM v_daily_coverage
)
WHERE neighbour_median > 0 AND row_count < neighbour_median * 0.5
ORDER BY date;
