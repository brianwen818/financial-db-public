"""Build ``finmind.duckdb`` from the SQL files in ``sql/``.

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

import logging
from pathlib import Path

import duckdb

from finmind_pipeline.settings import PROJECT_ROOT, get_settings

log = logging.getLogger(__name__)

SQL_DIR = PROJECT_ROOT / "sql"
DB_ROOT_TOKEN = "{{DB_ROOT}}"


def sql_files() -> list[Path]:
    """Every .sql file under sql/, excluding queries/, in load order.

    Sorted on the *filename*, not the path: the numeric prefixes are the
    dependency order, and sorting whole paths would order by directory first,
    so a later ``sql/marts/30_x.sql`` would execute before ``sql/views/20_x.sql``.

    ``queries/`` is excluded at any depth -- those files are documentation and
    contain bare SELECTs, which would be executed as DDL if they were picked up.
    """
    files = [
        p
        for p in SQL_DIR.rglob("*.sql")
        if "queries" not in p.relative_to(SQL_DIR).parts
    ]
    files.sort(key=lambda p: p.name)
    if not files:
        raise FileNotFoundError(f"no SQL files found under {SQL_DIR}")
    return files


def render(sql: str, db_root: Path) -> str:
    # Forward slashes: DuckDB treats backslashes as escapes inside string literals.
    return sql.replace(DB_ROOT_TOKEN, str(db_root).replace("\\", "/"))


def build(db_path: Path | None = None, db_root: Path | None = None) -> Path:
    """(Re)create every view. Returns the database path."""
    settings = get_settings()
    db_path = Path(db_path or settings.duckdb_path)
    db_root = Path(db_root or settings.db_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)

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
