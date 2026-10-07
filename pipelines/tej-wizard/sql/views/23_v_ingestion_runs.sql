-- Run history and lineage, read straight from the JSONL manifests each job
-- appends. Turns "what ran, when, and did it work" into a SQL question.
CREATE OR REPLACE VIEW v_ingestion_runs AS
SELECT
    job,
    status,
    CAST(started_at  AS TIMESTAMP) AS started_at,
    CAST(finished_at AS TIMESTAMP) AS finished_at,
    duration_seconds,
    host,
    metrics,
    errors,
    len(errors) AS error_count
FROM read_json_auto('{{DB_ROOT}}/logs/runs/*.jsonl',
                    format = 'newline_delimited', union_by_name = true);
