"""Raw workbook -> processed transform for the daily index-constituent dataset.

Output layout is Hive-partitioned on ``year``/``month`` under
``processed/index-constituents/``, the same shape as the price dataset so the
same partition-pruning rule applies to both.

**This is always a full rebuild, never an append**, for the same reason as the
price side: a refreshed workbook is a whole year of one index, so there is no
"just today's partition" to write. Parsing all 48 workbooks takes a few seconds,
so the distinction costs nothing, and the staged directory swap keeps the live
data queryable throughout.

The raw layout differs from the price dataset and drives everything here:

    raw/daily-index-constituents/
        TWN50/2002.xlsx ... 2026.xlsx
        TM100/2004.xlsx ... 2026.xlsx

Only ``<YYYY>.xlsx`` is ingested. Anything else in those directories -- a
``26-test.xlsx`` left over from a manual experiment, say -- is a copy of data
that is already in a year file, and ingesting it would duplicate every date it
covers. The filter is deliberate rather than incidental.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterable
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from tej_pipeline.processing import index_schema as IS
from tej_pipeline.processing.xlsx_reader import WorkbookFormatError, read_index_workbook
from tej_pipeline.settings import get_settings
from tej_pipeline.utils.fsutil import excel_lock_files
from tej_pipeline.utils.parquet_io import (
    StagedDirectory,
    atomic_write_table,
    partition_path,
    partition_runs,
)

log = logging.getLogger(__name__)

DATASET = "index_constituents"

# A data workbook is exactly one calendar year. Nothing else is ingested.
YEAR_FILE = re.compile(r"^(\d{4})\.xlsx$")


class RawLayerBusyError(RuntimeError):
    """A workbook is open in Excel, so the raw layer is not safe to read."""


def index_dirs(raw_dir: Path) -> list[Path]:
    """Every index subdirectory under the raw root, in name order."""
    return sorted(p for p in Path(raw_dir).iterdir() if p.is_dir())


def index_workbooks(raw_dir: Path) -> list[tuple[str, Path]]:
    """Every ``(index_id, path)`` data workbook, oldest year first per index."""
    out: list[tuple[str, Path]] = []
    for directory in index_dirs(raw_dir):
        for path in sorted(directory.glob("*.xlsx")):
            if YEAR_FILE.match(path.name):
                out.append((directory.name, path))
            elif not path.name.startswith("~$"):
                log.info("skipping non-year workbook %s/%s", directory.name, path.name)
    return out


def workbook_year(path: Path) -> int:
    """``2026.xlsx`` -> ``2026``."""
    m = YEAR_FILE.match(Path(path).name)
    if m is None:
        raise ValueError(f"{path.name} is not a <YYYY>.xlsx workbook")
    return int(m.group(1))


def current_year_workbooks(raw_dir: Path, year: int) -> dict[str, Path]:
    """``{index_id: path}`` for one calendar year -- what a refresh updates.

    Missing entries are the caller's problem to report: on the first trading
    day of a new year the workbook does not exist yet and has to be created
    from the add-in once, by hand.
    """
    return {
        index_id: path
        for index_id, path in index_workbooks(raw_dir)
        if workbook_year(path) == year
    }


def assert_raw_readable(raw_dir: Path) -> None:
    """Refuse to read the raw layer while Excel holds a workbook open.

    Same reasoning as the price dataset: a workbook caught mid-save parses as a
    genuinely different file, so this fails loudly rather than quietly ingesting
    a torn snapshot. Lock files live beside the workbook, so every index
    subdirectory has to be checked, not just the root.
    """
    locks = [p for d in index_dirs(raw_dir) for p in excel_lock_files(d)]
    if locks:
        names = ", ".join(f"{p.parent.name}/{p.name.removeprefix('~$')}" for p in locks)
        raise RawLayerBusyError(
            f"{len(locks)} workbook(s) open in Excel: {names}. Close them and re-run."
        )


def load_raw(
    raw_dir: Path,
    indices: Iterable[str] | None = None,
    ingested_at: dt.datetime | None = None,
) -> tuple[pa.Table, list[tuple[str, str]]]:
    """Parse every workbook into one table. Returns ``(table, errors)``.

    A workbook that fails to parse is reported rather than aborting the run, so
    one bad year cannot block a rebuild of the other 47.
    """
    assert_raw_readable(raw_dir)
    wanted = set(indices) if indices else None
    ts = ingested_at or dt.datetime.now(dt.UTC)

    tables: list[pa.Table] = []
    errors: list[tuple[str, str]] = []
    for index_id, path in index_workbooks(raw_dir):
        if wanted is not None and index_id not in wanted:
            continue
        label = f"{index_id}/{path.name}"
        try:
            table = read_index_workbook(path, index_id, ingested_at=ts)
        except (WorkbookFormatError, ValueError, KeyError, OSError) as exc:
            errors.append((label, f"{type(exc).__name__}: {exc}"))
            log.error("failed to read %s: %s", label, exc)
            continue
        log.info("read %s: %d rows", label, table.num_rows)
        tables.append(table)

    if not tables:
        return pa.table([], schema=IS.PROCESSED_SCHEMA), errors
    return pa.concat_tables(tables), errors


def assert_unique_key(table: pa.Table) -> None:
    """``(index_id, date, stock_id)`` must be unique across the whole dataset.

    Measured unique over all 832,648 rows on 2026-09-05. A duplicate means one
    trading day was collected into two year files -- the failure mode a stray
    ``26-test.xlsx`` in the raw directory would cause -- and would double-weight
    that day in every membership query.
    """
    if table.num_rows == 0:
        return
    parts = [
        pc.cast(table.column(name).combine_chunks(), pa.string())
        for name in IS.KEY_COLUMNS
    ]
    key = parts[0]
    for part in parts[1:]:
        key = pc.binary_join_element_wise(key, part, "|")
    n_unique = len(pc.unique(key))
    if n_unique != table.num_rows:
        raise ValueError(
            f"{'/'.join(IS.KEY_COLUMNS)} is not unique: "
            f"{table.num_rows} rows, {n_unique} distinct keys"
        )


def assert_years_do_not_overlap(table: pa.Table) -> None:
    """No date may appear in two workbooks of the same index.

    The year files partition the history exactly, and a refresh that seeded a
    date into the wrong year's workbook would break that silently -- the key
    check above would still pass, because the duplicate rows would differ in
    nothing but provenance.
    """
    if table.num_rows == 0:
        return
    seen: dict[tuple[str, dt.date], str] = {}
    index_ids = table.column("index_id").to_pylist()
    dates = table.column("date").to_pylist()
    files = table.column("source_file").to_pylist()
    for index_id, date, source in zip(index_ids, dates, files, strict=True):
        key = (index_id, date)
        first = seen.setdefault(key, source)
        if first != source:
            raise ValueError(
                f"{index_id} {date} appears in both {first} and {source}; "
                "the year workbooks must not overlap"
            )


def rebuild(indices: Iterable[str] | None = None) -> dict[str, int | str]:
    """Rebuild every processed partition from the raw workbooks.

    Returns a summary dict suitable for a run manifest.
    """
    settings = get_settings()
    cfg = settings.dataset(DATASET)
    raw_dir = settings.raw_dir_for(DATASET)
    live_dir = settings.processed_dir_for(DATASET)
    grain = cfg.partition

    table, errors = load_raw(raw_dir, indices=indices)
    if table.num_rows == 0:
        raise RuntimeError(
            f"no rows parsed from {raw_dir}; refusing to swap in an empty dataset"
        )

    assert_unique_key(table)
    assert_years_do_not_overlap(table)

    # Sort so partitions are contiguous, and so rows inside a file group by
    # index then security -- which is what makes the Parquet dictionary
    # encoding of index_id, stock_id and stock_name effective.
    table = table.sort_by(
        [("date", "ascending"), ("index_id", "ascending"), ("stock_id", "ascending")]
    )

    files = written = 0
    with StagedDirectory(live_dir) as staging:
        for rep_date, start, end in partition_runs(table, grain):
            chunk = table.slice(start, end - start)
            written += atomic_write_table(chunk, partition_path(staging, rep_date, grain))
            files += 1

    summary: dict[str, int | str] = {
        "dataset": DATASET,
        "partition_grain": grain,
        "workbooks": len(index_workbooks(raw_dir)),
        "indices": len(pc.unique(table.column("index_id").combine_chunks())),
        "securities": len(pc.unique(table.column("stock_id").combine_chunks())),
        "rows": written,
        "partitions": files,
        "date_min": str(pc.min(table.column("date")).as_py()),
        "date_max": str(pc.max(table.column("date")).as_py()),
        "read_errors": len(errors),
    }
    log.info(
        "rebuilt %s: %d rows in %d %s partitions (%s..%s), %d read errors",
        DATASET,
        written,
        files,
        grain,
        summary["date_min"],
        summary["date_max"],
        len(errors),
    )
    return summary


# --- universe helpers, read from the views -----------------------------------
#
# These read the DuckDB build rather than the raw layer, because "who is in the
# index right now" and "who has no price history" are exactly what the views
# already compute. They need `rebuild()` to have run at least once.


def _query(sql: str) -> list[tuple]:
    import duckdb

    settings = get_settings()
    if not settings.duckdb_path.exists():
        raise FileNotFoundError(
            f"{settings.duckdb_path} does not exist; run scripts/build_db.py first"
        )
    con = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def current_members() -> set[str]:
    """Securities currently in TWN50 or TM100.

    This is the daily refresh scope for the price workbooks. A security that
    has left both indices keeps whatever history it had -- its adjusted prices
    still move on every ex-dividend, so the weekly full pass is what keeps them
    right, not the daily one.
    """
    return {row[0] for row in _query("SELECT stock_id FROM v_index_membership WHERE is_current")}


def members_without_prices() -> list[tuple[str, str]]:
    """``(stock_id, stock_name)`` for index members with no price workbook.

    The gap this database has to close before a TM100 cross-section can be
    priced at all. Ordered so the TWN50 members come first: those are the ones
    that make an existing 0050 backtest wrong rather than merely incomplete.
    """
    return [
        (row[0], row[1])
        for row in _query(
            "SELECT stock_id, stock_name FROM v_index_universe "
            "WHERE NOT has_price_history ORDER BY in_twn50 DESC, stock_id"
        )
    ]
