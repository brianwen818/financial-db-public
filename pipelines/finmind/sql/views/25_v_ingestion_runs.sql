-- Run history and lineage, read straight from the JSONL manifests each job
-- appends. Turns "what ran, when, and did it work" into a SQL question.
--
-- union_by_name absorbs new `metrics` keys as jobs gain them, but it cannot
-- absorb a type change: if one manifest ever writes `errors` as a string rather
-- than a list, or omits it entirely, `len(errors)` breaks the whole view -- and
-- because this is a view over a glob, it breaks at read time, not at build
-- time. RunManifest always emits `errors` as a list for that reason.
--
-- The glob also grows without bound; logs/runs/ is never pruned.
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
