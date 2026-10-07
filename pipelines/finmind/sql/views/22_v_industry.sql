-- Industry supply-chain mapping.
--
-- Many-to-many: a stock carries 1..61 rows. The key is
-- (stock_id, industry, sub_industry) -- NOT stock_id, and NOT
-- (stock_id, snapshot_date). Aggregate before joining to prices or you will
-- fan out the row count.
--
-- snapshot_date is "last seen upstream", not an effective date: upstream
-- re-stamps a row every time it re-crawls it, so the processed layer collapses
-- on the natural key and keeps max(). Filtering on it does not give you a
-- point-in-time mapping -- there is no history here.
CREATE OR REPLACE VIEW v_industry AS
SELECT stock_id, industry, sub_industry, snapshot_date
FROM read_parquet('{{DB_ROOT}}/processed/tw-industry/tw_industry.parquet');

-- One row per stock, industries collapsed to a list. Safe to join 1:1.
CREATE OR REPLACE VIEW v_industry_by_stock AS
SELECT
    stock_id,
    list_distinct(list(industry))     AS industries,
    list_distinct(list(sub_industry)) AS sub_industries,
    max(snapshot_date)                AS last_seen
FROM v_industry
GROUP BY stock_id;
