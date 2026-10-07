-- Coverage and reconciliation for the index-constituent dataset.

-- Sessions each index has published a constituent list for. Unlike the price
-- side this IS effectively an exchange calendar: TEJ publishes a full list on
-- every session the index was calculated, so a missing date here is a missing
-- refresh, not a security that happened not to trade.
--
-- It runs one session ahead of v_trading_calendar, because a list dated D is
-- published after D-1's close.
CREATE OR REPLACE VIEW v_index_calendar AS
SELECT DISTINCT index_id, date FROM v_index_constituents;

CREATE OR REPLACE VIEW v_index_last_trading_day AS
SELECT index_id, max(date) AS date
FROM v_index_constituents
GROUP BY index_id;

-- How many constituents each index carried on each session.
--
-- THE COUNT IS NOT FIXED. Measured: TWN50 runs 46-51 (49 members through 2002,
-- 51 on two days in 2010 and one in 2021) and TM100 runs 98-102. A check that
-- asserts exactly 50 or 100 will fail on correct data; compare against the
-- index's own local median instead, which is what v_thin_index_days does.
CREATE OR REPLACE VIEW v_index_daily_coverage AS
SELECT
    index_id,
    date,
    count(*)                 AS constituents,
    count(DISTINCT stock_id) AS distinct_stocks,
    round(sum(prev_weight_pct), 4) AS total_weight_pct,
    max(ingested_at)         AS last_ingested
FROM v_index_constituents
GROUP BY index_id, date;

-- A session whose constituent count is far from its neighbours -- the signature
-- of a refresh that landed half a day. The window is per index, so TWN50's 50
-- and TM100's 100 are never compared with each other.
CREATE OR REPLACE VIEW v_thin_index_days AS
SELECT index_id, date, constituents, neighbour_median,
       round(constituents::DOUBLE / nullif(neighbour_median, 0), 3) AS ratio
FROM (
    SELECT index_id, date, constituents,
           median(constituents) OVER (
               PARTITION BY index_id ORDER BY date
               ROWS BETWEEN 5 PRECEDING AND 5 FOLLOWING
           ) AS neighbour_median
    FROM v_index_daily_coverage
)
WHERE neighbour_median > 0 AND constituents < neighbour_median * 0.9
ORDER BY index_id, date;

-- Sessions where the index list exists but the price layer has no bar for a
-- constituent. Every row here is a hole a point-in-time backtest would hit:
-- the stock was in the index that day and this database cannot price it.
--
-- Rows for securities with no workbook at all are the bulk of it; join
-- v_index_universe.has_price_history to tell "no workbook" apart from "workbook
-- exists but is missing that day".
CREATE OR REPLACE VIEW v_index_price_gaps AS
SELECT
    c.index_id,
    c.date,
    c.stock_id,
    c.stock_name,
    u.has_price_history
FROM v_index_constituents c
JOIN v_index_universe u USING (stock_id)
LEFT JOIN v_adj_daily_prices p
  ON p.stock_id = c.stock_id AND p.date = c.date
WHERE p.date IS NULL
  AND c.date <= (SELECT max(date) FROM v_adj_daily_prices);
