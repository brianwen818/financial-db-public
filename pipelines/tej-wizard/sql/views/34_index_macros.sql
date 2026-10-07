-- Date-range helper that prunes partitions, and the point-in-time universe.
--
-- Same rule as adj_daily_prices_between(): the Hive keys are `year` and
-- `month`, DuckDB cannot infer either from a `date` predicate, so a plain
-- `WHERE date BETWEEN ...` opens every Parquet footer in the dataset.
--
--     SELECT * FROM index_constituents_between('2024-01-01', '2024-03-31')
--     WHERE index_id = 'TWN50';
CREATE OR REPLACE MACRO index_constituents_between(lo, hi) AS TABLE
SELECT * FROM v_index_constituents
WHERE year BETWEEN year(CAST(lo AS DATE)) AND year(CAST(hi AS DATE))
  AND date BETWEEN CAST(lo AS DATE) AND CAST(hi AS DATE);

-- The constituents of one index on one session -- the correct historical
-- universe for a backtest as at that date.
--
--     SELECT * FROM index_members_on('TWN50', '2015-06-30') ORDER BY weight_pct DESC;
--
-- Falls back to nothing rather than to the nearest date: an empty result means
-- that session had no published list, which is a fact worth seeing rather than
-- papering over with the previous day's members.
CREATE OR REPLACE MACRO index_members_on(idx, as_of) AS TABLE
SELECT stock_id, stock_name,
       prev_weight_pct AS weight_pct,
       index_factor, free_float_factor, cap_factor, shares, prev_adj_close
FROM v_index_constituents
WHERE year = year(CAST(as_of AS DATE))
  AND date = CAST(as_of AS DATE)
  AND index_id = idx;
