"""Structured run records for lineage and reconciliation reporting.

Each job appends one JSON object per run to
``db/tej-wizard/logs/runs/<date>_<job>.jsonl``. DuckDB reads the whole directory
as a view (``v_ingestion_runs``), which turns "what ran, when, and what did it
produce" into a SQL question rather than a log-grepping exercise.

JSONL rather than a single JSON document so that a crashed run still leaves
valid, parseable history behind.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import platform
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class RunManifest:
    """Collect metrics for one job run and append them on exit.

    Usage::

        with RunManifest("rebuild") as run:
            run.metric("workbooks", 120)
            run.event("parsed", stock_id="2330", rows=6409)
    """

    def __init__(self, job: str, runs_dir: Path | None = None) -> None:
        from tej_pipeline.settings import get_settings

        settings = get_settings()
        self.job = job
        self.runs_dir = Path(runs_dir) if runs_dir else settings.logs_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

        self.started_at = dt.datetime.now(dt.UTC)
        self._t0 = time.monotonic()
        self.metrics: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.status = "running"

    # ------------------------------------------------------------------
    def metric(self, name: str, value: Any) -> None:
        self.metrics[name] = value

    def incr(self, name: str, by: int = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + by

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append({"kind": kind, **fields})

    def error(self, message: str) -> None:
        self.errors.append(message)
        log.error("%s", message)

    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.runs_dir / f"{self.started_at.date().isoformat()}_{self.job}.jsonl"

    def _record(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": dt.datetime.now(dt.UTC).isoformat(),
            "duration_seconds": round(time.monotonic() - self._t0, 3),
            "host": platform.node(),
            "pid": os.getpid(),
            "metrics": self.metrics,
            "events": self.events,
            "errors": self.errors,
        }

    def write(self) -> Path:
        record = self._record()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return self.path

    # ------------------------------------------------------------------
    def __enter__(self) -> RunManifest:
        log.info("=== %s starting ===", self.job)
        return self

    def __exit__(self, exc_type: type | None, exc: BaseException | None, _tb: object) -> None:
        if exc_type is not None:
            self.status = "failed"
            self.errors.append(f"{exc_type.__name__}: {exc}")
        elif self.errors:
            self.status = "completed_with_errors"
        else:
            self.status = "success"
        path = self.write()
        log.info(
            "=== %s %s in %.1fs (%s) ===",
            self.job,
            self.status,
            time.monotonic() - self._t0,
            path.name,
        )
