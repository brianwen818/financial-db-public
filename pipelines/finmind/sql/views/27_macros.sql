-- Date-range helpers that prune partitions.
--
-- Measured on the full 8,053-file dataset: a plain `WHERE date BETWEEN ...`
-- opens every Parquet footer (8,053 files, ~0.56s), because the Hive keys are
-- year and month and DuckDB cannot infer them from a `date` predicate. Adding
-- a `year BETWEEN` predicate prunes whole directories instead (~0.13s, 4.3x).
--
-- An arithmetic form such as `(year*100+month) BETWEEN ...` does NOT prune --
-- DuckDB will not push an expression over partition columns down to the file
-- lister. Keep the predicate a plain comparison on `year`.
--
-- v_prices_combined joins both price datasets, so an unpruned range query there
-- pays the footer cost twice (measured: 1.40s for a quarter, versus 0.14s
-- pruned). It carries year/month for exactly this reason.
--
-- Use these instead of remembering the rule:
--     SELECT * FROM daily_prices_between('2024-01-01', '2024-03-31')
--     WHERE stock_id = '2330';

CREATE OR REPLACE MACRO daily_prices_between(lo, hi) AS TABLE
SELECT * FROM v_daily_prices
WHERE year BETWEEN year(CAST(lo AS DATE)) AND year(CAST(hi AS DATE))
  AND date BETWEEN CAST(lo AS DATE) AND CAST(hi AS DATE);

CREATE OR REPLACE MACRO adj_daily_prices_between(lo, hi) AS TABLE
SELECT * FROM v_adj_daily_prices
WHERE year BETWEEN year(CAST(lo AS DATE)) AND year(CAST(hi AS DATE))
  AND date BETWEEN CAST(lo AS DATE) AND CAST(hi AS DATE);

CREATE OR REPLACE MACRO prices_combined_between(lo, hi) AS TABLE
SELECT * FROM v_prices_combined
WHERE year BETWEEN year(CAST(lo AS DATE)) AND year(CAST(hi AS DATE))
  AND date BETWEEN CAST(lo AS DATE) AND CAST(hi AS DATE);
