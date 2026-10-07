-- Date-range helper that prunes partitions.
--
-- The Hive keys are `year` and `month`, and DuckDB cannot infer either from a
-- `date` predicate, so a plain `WHERE date BETWEEN ...` opens every Parquet
-- footer in the dataset. Constraining `year` prunes whole directories instead.
--
-- An arithmetic form such as `(year*100+month) BETWEEN ...` does NOT prune --
-- DuckDB will not push an expression over partition columns down to the file
-- lister. Keep the predicate a plain comparison on `year`.
--
--     SELECT * FROM adj_daily_prices_between('2024-01-01', '2024-03-31')
--     WHERE stock_id = '2330';
CREATE OR REPLACE MACRO adj_daily_prices_between(lo, hi) AS TABLE
SELECT * FROM v_adj_daily_prices
WHERE year BETWEEN year(CAST(lo AS DATE)) AND year(CAST(hi AS DATE))
  AND date BETWEEN CAST(lo AS DATE) AND CAST(hi AS DATE);
