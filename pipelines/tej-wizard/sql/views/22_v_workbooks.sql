-- The raw layer's own state, as scanned from the xlsx containers.
--
-- This exists because the update path is a GUI add-in, not an HTTP call. A
-- failed refresh raises nothing the pipeline can catch -- an expired TEJ
-- session, or a workbook left open, just leaves yesterday's numbers in place.
-- The only reliable evidence is the file's own modification time set against
-- the newest bar it produced.
CREATE OR REPLACE VIEW v_workbooks AS
SELECT
    source_file, stock_id,
    file_bytes, modified_at,
    sheet_name, defined_name, defined_range, declared_rows,
    tej_table, tej_field_count, descending,
    query_matches_contract,
    scanned_at
FROM read_parquet('{{DB_ROOT}}/processed/workbook-inventory/workbook_inventory.parquet');

-- Did the last refresh actually land? Joins each workbook to what it produced.
--
-- rows_delta is the add-in's own declared block size minus the rows that
-- reached the processed layer; anything non-zero means the parse and the
-- workbook disagree about how much data is there.
--
-- days_behind counts observed trading days between a security's newest bar and
-- the newest bar anywhere in the database. A security that genuinely stopped
-- trading (delisted, or dropped out of the index set) sits permanently behind,
-- so read it together with is_current rather than as a fault on its own.
CREATE OR REPLACE VIEW v_workbook_status AS
SELECT
    w.stock_id,
    s.stock_name,
    w.source_file,
    w.modified_at,
    s.first_date,
    s.last_date,
    s.trading_days,
    w.declared_rows,
    w.declared_rows - s.trading_days AS rows_delta,
    s.is_current,
    (SELECT count(*) FROM v_trading_calendar c WHERE c.date > s.last_date) AS days_behind,
    w.query_matches_contract,
    round(w.file_bytes / 1024.0 / 1024.0, 2) AS file_mb
FROM v_workbooks w
LEFT JOIN v_securities s USING (stock_id);
