"""Shared helpers for the grad-school-application analyses.

Everything here is read-only against the warehouses: both DuckDB files are
attached READ_ONLY into an in-memory connection and every intermediate result
is a TEMP table, so nothing under ``db/`` is ever written.

Do not run these while a scheduled pipeline job is active (FinMind 18:30 /
Sun 02:00, TEJ Sun 03:00 for ~8 h): an open reader blocks the job's view
rebuild and its staged directory swap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]  # repo root (db/ is not shipped)
DB = ROOT / "db"
TEJ_DB = DB / "tej-wizard" / "tej_wizard.duckdb"
FINMIND_DB = DB / "finmind" / "finmind.duckdb"
FINMIND_RAW = DB / "finmind" / "raw"

OUT = Path(__file__).resolve().parents[1] / "outputs"
TABLES = OUT / "tables"
FIGURES = OUT / "figures"

# TEJ market codes that are exchange-auction markets. REG / PSB are 興櫃
# (negotiated prices, no daily limit) and are excluded from return diagnostics.
LISTED = "('TSE','OTC','TIB')"


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("PRAGMA threads=6")
    con.execute(f"ATTACH '{TEJ_DB.as_posix()}' AS tej (READ_ONLY)")
    con.execute(f"ATTACH '{FINMIND_DB.as_posix()}' AS fm (READ_ONLY)")
    return con


def setup_output() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 60)
    pd.set_option("display.max_rows", 300)
    TABLES.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)


def save(con: duckdb.DuckDBPyConnection, name: str, sql: str, quiet: bool = False) -> pd.DataFrame:
    """Run ``sql``, write it to outputs/tables/<name>.csv and echo it."""
    df = con.execute(sql).df()
    df.to_csv(TABLES / f"{name}.csv", index=False, encoding="utf-8-sig")
    print(f"\n### {name}  ({len(df)} rows)")
    if not quiet:
        print(df.to_string(index=False))
    return df
