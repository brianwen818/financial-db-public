-- The index-constituent raw layer's own state, scanned from the xlsx containers.
--
-- Same reasoning as v_workbooks: the update path is a GUI add-in, so a failed
-- refresh raises nothing the pipeline can catch. The file's modification time
-- set against the newest row it produced is the only reliable evidence.
CREATE OR REPLACE VIEW v_index_workbooks AS
SELECT
    source_file, index_id, year,
    file_bytes, modified_at,
    sheet_name, defined_name, defined_range, declared_rows,
    tej_table, tej_field_count, descending,
    query_matches_contract,
    scanned_at
FROM read_parquet(
    '{{DB_ROOT}}/processed/index-workbook-inventory/index_workbook_inventory.parquet');

-- Did the last refresh actually land, per index-year?
--
-- rows_delta is the add-in's own declared block size minus the rows that
-- reached the processed layer; non-zero means the parse and the workbook
-- disagree about how much data is there.
--
-- Only the current year's workbook is expected to move. A closed year that
-- stops changing is correct, not stale, so is_current_year is what separates
-- "nothing to do" from "behind".
CREATE OR REPLACE VIEW v_index_workbook_status AS
SELECT
    w.index_id,
    w.year,
    w.source_file,
    w.modified_at,
    p.first_date,
    p.last_date,
    p.sessions,
    p.row_count AS parsed_rows,
    w.declared_rows,
    w.declared_rows - p.row_count AS rows_delta,
    w.year = year(current_date) AS is_current_year,
    w.query_matches_contract,
    round(w.file_bytes / 1024.0 / 1024.0, 2) AS file_mb
FROM v_index_workbooks w
LEFT JOIN (
    SELECT
        index_id,
        year,
        min(date)                AS first_date,
        max(date)                AS last_date,
        count(DISTINCT date)     AS sessions,
        count(*)                 AS row_count
    FROM v_index_constituents
    GROUP BY index_id, year
) p USING (index_id, year);
