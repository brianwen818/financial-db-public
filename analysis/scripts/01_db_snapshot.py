"""Measured size, coverage and health of both warehouses, as of the run time.

Writes one CSV per table under outputs/tables/snapshot_*.csv. Documents quote
these numbers instead of the README snapshots, which drift with every update.
"""

from common import connect, save, setup_output

setup_output()
con = connect()

save(con, "snapshot_price_tables", """
SELECT 'finmind.v_daily_prices' AS view_name, count(*) AS n_rows, count(DISTINCT stock_id) AS securities,
       min(date) AS first_date, max(date) AS last_date, count(DISTINCT date) AS trading_days
FROM fm.v_daily_prices
UNION ALL
SELECT 'finmind.v_adj_daily_prices', count(*), count(DISTINCT stock_id), min(date), max(date), count(DISTINCT date)
FROM fm.v_adj_daily_prices
UNION ALL
SELECT 'tej.v_adj_daily_prices', count(*), count(DISTINCT stock_id), min(date), max(date), count(DISTINCT date)
FROM tej.v_adj_daily_prices
UNION ALL
SELECT 'tej.v_index_constituents', count(*), count(DISTINCT stock_id), min(date), max(date), count(DISTINCT date)
FROM tej.v_index_constituents
""")

save(con, "snapshot_index_constituents", """
SELECT index_id, count(*) AS n_rows, count(DISTINCT stock_id) AS securities, min(date) AS first_date,
       max(date) AS last_date, count(DISTINCT date) AS sessions
FROM tej.v_index_constituents GROUP BY 1 ORDER BY 1
""")

save(con, "snapshot_finmind_universe", """
SELECT coalesce(type, '(delisted, recovered)') AS type, is_delisted, is_index, count(*) AS securities
FROM fm.v_securities_all GROUP BY ALL ORDER BY 2, 1, 3
""")

save(con, "snapshot_finmind_delisted_coverage", """
SELECT count(*) AS delisted_recovered,
       count(*) FILTER (WHERE stock_id IN (SELECT DISTINCT stock_id FROM fm.v_daily_prices)) AS with_unadjusted,
       count(*) FILTER (WHERE stock_id IN (SELECT DISTINCT stock_id FROM fm.v_adj_daily_prices)) AS with_adjusted
FROM fm.v_delisted_securities
""")

save(con, "snapshot_tej_universe", """
SELECT is_current, count(*) AS securities, min(first_date) AS first_date, max(last_date) AS last_date
FROM tej.v_securities GROUP BY 1 ORDER BY 1
""")

save(con, "snapshot_tej_market_rows", """
SELECT market, count(*) AS n_rows, count(DISTINCT stock_id) AS securities
FROM tej.v_adj_daily_prices GROUP BY 1 ORDER BY 2 DESC
""")

save(con, "snapshot_rows_per_year", """
WITH f AS (SELECT year AS y, count(*) AS finmind_unadjusted FROM fm.v_daily_prices GROUP BY 1),
     a AS (SELECT year AS y, count(*) AS finmind_adjusted FROM fm.v_adj_daily_prices GROUP BY 1),
     t AS (SELECT year AS y, count(*) AS tej_adjusted FROM tej.v_adj_daily_prices GROUP BY 1)
SELECT y AS year, finmind_unadjusted, finmind_adjusted, tej_adjusted
FROM f FULL JOIN a USING (y) FULL JOIN t USING (y) ORDER BY 1
""")

save(con, "snapshot_ingestion_runs", """
SELECT 'finmind' AS pipeline, job, status, count(*) AS runs, min(started_at) AS first_run, max(started_at) AS last_run
FROM fm.v_ingestion_runs GROUP BY ALL
UNION ALL
SELECT 'tej', job, status, count(*), min(started_at), max(started_at) FROM tej.v_ingestion_runs GROUP BY ALL
ORDER BY 1, 2, 3
""")

save(con, "snapshot_health", """
SELECT (SELECT count(*) FROM fm.v_missing_trading_days) AS fm_missing_days,
       (SELECT count(*) FROM fm.v_thin_trading_days) AS fm_thin_days,
       (SELECT count(*) FROM fm.v_known_empty_days) AS fm_known_empty_days,
       (SELECT count(*) FROM fm.v_trading_calendar) AS fm_calendar_days,
       (SELECT count(*) FROM tej.v_thin_trading_days) AS tej_thin_days,
       (SELECT count(*) FROM tej.v_security_gaps) AS tej_gap_rows,
       (SELECT count(DISTINCT stock_id) FROM tej.v_security_gaps) AS tej_gap_securities,
       (SELECT count(*) FROM tej.v_index_price_gaps) AS tej_index_price_gaps,
       (SELECT count(*) FROM tej.v_index_universe WHERE NOT has_price_history) AS tej_members_without_prices,
       (SELECT count(*) FROM tej.v_workbook_status WHERE rows_delta <> 0) AS tej_workbook_rows_delta,
       (SELECT count(*) FROM tej.v_workbook_status WHERE NOT query_matches_contract) AS tej_contract_mismatch
""")

con.close()
