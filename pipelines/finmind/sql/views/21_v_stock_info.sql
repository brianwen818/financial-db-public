-- Security master.
--
-- This is a cumulative snapshot log, not a current-state table: the same stock
-- appears under several industry_category values across scrape dates because
-- the label was renamed upstream. 4,321 rows cover 3,147 securities.
--
-- snapshot_date is the date this exact content was FIRST seen, not the date it
-- was last crawled: upstream re-stamps most rows on every pull, so processing
-- collapses them on content and keeps min(). A row dated today is a genuine
-- change, not a re-crawl.
--
-- snapshot_date is NULL for the 32 index pseudo-tickers (TAIEX, TPEx and 30
-- sector indices) -- FinMind sends the literal string "None" for those, which
-- processing converts to a real NULL.
CREATE OR REPLACE VIEW v_stock_info_history AS
SELECT
    snapshot_date,
    stock_id,
    stock_name,
    industry_category,
    industry_category_norm,
    type,
    is_index
FROM read_parquet('{{DB_ROOT}}/processed/tw-stock-info/tw_stock_info.parquet');

-- Current state: newest snapshot per security. Ties are broken on
-- industry_category so the result is deterministic.
CREATE OR REPLACE VIEW v_stock_info_latest AS
SELECT * EXCLUDE (rn) FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY stock_id
        ORDER BY snapshot_date DESC NULLS LAST, industry_category
    ) AS rn
    FROM v_stock_info_history
) WHERE rn = 1;

-- Tradable securities only -- excludes the 32 index pseudo-tickers.
CREATE OR REPLACE VIEW v_securities AS
SELECT * FROM v_stock_info_latest WHERE NOT is_index;

-- The 32 index series, which do carry real OHLCV history
-- (TAIEX goes back to 1999 with 6,849 rows).
CREATE OR REPLACE VIEW v_indices AS
SELECT * FROM v_stock_info_latest WHERE is_index;

-- Securities that traded but are absent from the security master, because
-- TaiwanStockInfo is current-state and they delisted before the first snapshot
-- (2026-08-26). Recovered by sampling whole-market days; see
-- ingestion/universe_discovery.py.
--
-- stock_name and delisted_date are NULL for the codes only the sweep found:
-- TaiwanStockDelisting names roughly a third of them and nothing else upstream
-- carries a name. first_seen/last_seen are sampling observations, NOT listing
-- and delisting dates -- with one sample per month the true dates sit up to a
-- month outside that window.
CREATE OR REPLACE VIEW v_delisted_securities AS
SELECT
    stock_id,
    stock_name,
    delisted_date,
    shape,
    first_seen,
    last_seen,
    source
FROM read_parquet('{{DB_ROOT}}/processed/delisted-securities/delisted_securities.parquet');

-- Every security that ever traded: the master plus the delisted ones, one row
-- per stock_id. This is the correct thing to join prices against -- joining
-- v_stock_info_latest instead silently drops every delisted security, which is
-- how survivorship bias gets into a backtest.
--
-- Delisted rows carry no industry: use is_delisted to exclude them from any
-- industry-grouped analysis rather than letting them fall into a NULL bucket.
-- A security can be in both sides: the master is a cumulative log, so one that
-- delists from now on keeps its master row (with its name and industry) while
-- also entering v_delisted_securities. Those rows are kept once, on the master
-- side, so the richer metadata survives and the flag still flips.
CREATE OR REPLACE VIEW v_securities_all AS
SELECT
    s.stock_id,
    s.stock_name,
    s.industry_category,
    s.industry_category_norm,
    s.type,
    s.is_index,
    d.stock_id IS NOT NULL   AS is_delisted,
    d.delisted_date
FROM v_stock_info_latest s
LEFT JOIN v_delisted_securities d USING (stock_id)
UNION ALL
SELECT
    d.stock_id,
    d.stock_name,
    CAST(NULL AS VARCHAR)    AS industry_category,
    CAST(NULL AS VARCHAR)    AS industry_category_norm,
    CAST(NULL AS VARCHAR)    AS type,
    FALSE                    AS is_index,
    TRUE                     AS is_delisted,
    d.delisted_date
FROM v_delisted_securities d
WHERE NOT EXISTS (SELECT 1 FROM v_stock_info_latest s WHERE s.stock_id = d.stock_id);
