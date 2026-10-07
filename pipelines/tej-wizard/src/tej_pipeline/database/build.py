"""Build ``tej_wizard.duckdb`` from the SQL files in ``sql/``.

The database file holds **only views** over the Parquet layer, plus nothing
else. That keeps Parquet the single source of truth, keeps the file a few
hundred KB rather than gigabytes, and makes the database entirely disposable —
delete it and re-run this to get it back.

It also sidesteps DuckDB's single-writer lock: the scheduled jobs open the file
only briefly to redefine views, so an interactive DBeaver or notebook session
is rarely blocked. Read-only connections avoid the conflict entirely.

SQL files are executed in filename order, so the numeric prefixes encode the
dependency order between views.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import platform
from pathlib import Path

import duckdb

from tej_pipeline.settings import PROJECT_ROOT, get_settings

log = logging.getLogger(__name__)

SQL_DIR = PROJECT_ROOT / "sql"
DB_ROOT_TOKEN = "{{DB_ROOT}}"


def sql_files() -> list[Path]:
    """Every .sql file under sql/, excluding queries/, in load order."""
    files = [p for p in sorted(SQL_DIR.rglob("*.sql")) if p.parent.name != "queries"]
    if not files:
        raise FileNotFoundError(f"no SQL files found under {SQL_DIR}")
    return files


def render(sql: str, db_root: Path) -> str:
    # Forward slashes: DuckDB treats backslashes as escapes inside string literals.
    return sql.replace(DB_ROOT_TOKEN, str(db_root).replace("\\", "/"))


def _seed_run_log(db_root: Path) -> None:
    """Make sure ``logs/runs/`` holds at least one manifest.

    ``v_ingestion_runs`` reads ``logs/runs/*.jsonl``, and DuckDB raises rather
    than returning nothing when a glob matches no files. On a brand-new
    database there are no runs yet -- and the first build happens *inside* the
    first run, whose record is not written until that run exits -- so the view
    would fail to create on exactly the run that creates the database.

    Seeding one real record for the initialisation itself keeps the view's
    schema identical in both states, which a "no files" special case would not.
    """
    runs_dir = db_root / "logs" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    if any(runs_dir.glob("*.jsonl")):
        return

    now = dt.datetime.now(dt.UTC)
    record = {
        "job": "bootstrap",
        "status": "success",
        "started_at": now.isoformat(),
        "finished_at": now.isoformat(),
        "duration_seconds": 0.0,
        "host": platform.node(),
        "pid": os.getpid(),
        "metrics": {"reason": "initialised the run log so v_ingestion_runs can be created"},
        "events": [],
        "errors": [],
    }
    path = runs_dir / f"{now.date().isoformat()}_bootstrap.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info("seeded run log: %s", path.name)


def build(db_path: Path | None = None, db_root: Path | None = None) -> Path:
    """(Re)create every view. Returns the database path."""
    settings = get_settings()
    db_path = Path(db_path or settings.duckdb_path)
    db_root = Path(db_root or settings.db_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_run_log(db_root)

    con = duckdb.connect(str(db_path))
    try:
        for path in sql_files():
            statements = render(path.read_text(encoding="utf-8"), db_root)
            try:
                con.execute(statements)
            except duckdb.Error as exc:
                raise RuntimeError(f"failed executing {path.name}: {exc}") from exc
            log.info("applied %s", path.relative_to(SQL_DIR))

        views = [
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_type = 'VIEW' ORDER BY table_name"
            ).fetchall()
        ]
    finally:
        con.close()

    log.info("built %s with %d views: %s", db_path.name, len(views), ", ".join(views))
    return db_path


def verify(db_path: Path | None = None) -> dict[str, int | str | None]:
    """Query every view once so a broken definition surfaces immediately.

    ``CREATE VIEW`` only validates syntax; a view over a missing Parquet glob
    fails at first read. This runs that read.
    """
    settings = get_settings()
    db_path = Path(db_path or settings.duckdb_path)
    results: dict[str, int | str | None] = {}

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        views = [
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_type = 'VIEW' ORDER BY table_name"
            ).fetchall()
        ]
        for view in views:
            try:
                results[view] = con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
            except duckdb.Error as exc:
                results[view] = f"ERROR: {str(exc)[:120]}"
    finally:
        con.close()
    return results
