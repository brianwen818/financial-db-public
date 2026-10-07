-- Coverage and reconciliation.
--
-- There is no published trading calendar in this database -- TEJ delivers only
-- the rows a security actually traded. So the calendar is the union of every
-- date observed across all securities, which is exact for any day on which at
-- least one of them traded and undefined for any day none of them did. With
-- ~2,450 listed companies in the set that distinction does not matter in
-- practice, but it is the reason this is not called an exchange calendar.
CREATE OR REPLACE VIEW v_trading_calendar AS
SELECT DISTINCT date FROM v_adj_daily_prices;

-- Newest bar anywhere in the database.
CREATE OR REPLACE VIEW v_last_trading_day AS
SELECT max(date) AS date FROM v_adj_daily_prices;

CREATE OR REPLACE VIEW v_daily_coverage AS
SELECT
    date,
    count(*)                 AS row_count,
    count(DISTINCT stock_id) AS securities,
    max(ingested_at)         AS last_ingested
FROM v_adj_daily_prices
GROUP BY date;

-- A day whose row count is far below its neighbours. Note this is expected to
-- be quiet here rather than empty: the constituent set grows over the history,
-- so 2000 legitimately carries fewer securities than 2020. Comparing against a
-- local median rather than against the maximum is what keeps the early years
-- from all looking broken.
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

-- Days missing from a security's history that fall inside its own active span
-- and on which other securities did trade. A scattering is normal -- a trading
-- suspension, or a session in which that stock simply did not trade. A dense
-- block of them means a refresh landed short.
CREATE OR REPLACE VIEW v_security_gaps AS
SELECT
    s.stock_id,
    s.stock_name,
    c.date AS missing_date
FROM v_securities s
JOIN v_trading_calendar c
  ON c.date BETWEEN s.first_date AND s.last_date
LEFT JOIN v_adj_daily_prices p
  ON p.stock_id = s.stock_id AND p.date = c.date
WHERE p.date IS NULL;
