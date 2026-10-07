"""Raw workbook -> processed transform for the adjusted daily price dataset.

Output layout is Hive-partitioned on ``year``/``month`` under
``processed/adj-daily-prices/``, at either day or month grain
(``config/datasets.yml``, key ``partition``).

**This is always a full rebuild, never an append.** The refresh unit is a
workbook, and a workbook is one security's entire history — a single refreshed
ticker touches every date it covers, so there is no such thing as writing "just
today's partition". That is the opposite of the FinMind pipeline, where the
daily job creates exactly one new file per run and never revisits an old one.

The rebuild is cheap enough that the distinction costs little: parsing all 2,453
workbooks (~10.1M rows, 2026-09-29) takes about 15 minutes and writing the
partitions under a minute. The staged directory swap means the live data stays
queryable throughout.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from tej_pipeline.processing import schema as S
from tej_pipeline.processing.xlsx_reader import WorkbookFormatError, read_workbook
from tej_pipeline.settings import get_settings
from tej_pipeline.utils.fsutil import excel_lock_files
from tej_pipeline.utils.parquet_io import (
    StagedDirectory,
    atomic_write_table,
    partition_path,
    partition_runs,
)

log = logging.getLogger(__name__)


class RawLayerBusyError(RuntimeError):
    """A workbook is open in Excel, so the raw layer is not safe to read."""


def raw_workbooks(raw_dir: Path) -> list[Path]:
    """Every data workbook in ``raw_dir``, excluding Excel's lock files."""
    return sorted(p for p in Path(raw_dir).glob("*.xlsx") if not p.name.startswith("~$"))


def assert_raw_readable(raw_dir: Path) -> None:
    """Refuse to read the raw layer while Excel holds a workbook open.

    A workbook caught mid-save parses as a genuinely different file — during
    development one reported eight TEJ blocks and an empty header row — so this
    fails loudly rather than quietly ingesting a torn snapshot.
    """
    locks = excel_lock_files(raw_dir)
    if locks:
        names = ", ".join(p.name.removeprefix("~$") for p in locks)
        raise RawLayerBusyError(
            f"{len(locks)} workbook(s) open in Excel: {names}. Close them and re-run."
        )


def load_raw(
    raw_dir: Path,
    tickers: Iterable[str] | None = None,
    ingested_at: dt.datetime | None = None,
) -> tuple[pa.Table, list[tuple[str, str]]]:
    """Parse every workbook into one table. Returns ``(table, errors)``.

    A workbook that fails to parse is reported rather than aborting the run, so
    one corrupt file cannot block a rebuild of the other 119.
    """
    assert_raw_readable(raw_dir)
    wanted = set(tickers) if tickers else None
    ts = ingested_at or dt.datetime.now(dt.UTC)

    tables: list[pa.Table] = []
    errors: list[tuple[str, str]] = []
    for path in raw_workbooks(raw_dir):
        if wanted is not None and path.stem not in wanted:
            continue
        try:
            table = read_workbook(path, ingested_at=ts)
        except (WorkbookFormatError, ValueError, KeyError, OSError) as exc:
            errors.append((path.name, f"{type(exc).__name__}: {exc}"))
            log.error("failed to read %s: %s", path.name, exc)
            continue
        log.info("read %s: %d rows", path.name, table.num_rows)
        tables.append(table)

    if not tables:
        return pa.table([], schema=S.PROCESSED_SCHEMA), errors
    return pa.concat_tables(tables), errors


def assert_unique_key(table: pa.Table) -> None:
    """``(stock_id, date)`` must be unique across the whole dataset.

    Measured unique over all 651,011 rows on 2026-09-03. A duplicate means two
    workbooks cover the same security — most likely a file saved under a second
    name — and would silently double-count that security in every cross-section.
    """
    if table.num_rows == 0:
        return
    key = pc.binary_join_element_wise(
        table.column("stock_id").combine_chunks().cast(pa.string()),
        pc.cast(table.column("date").combine_chunks(), pa.string()),
        "|",
    )
    n_unique = len(pc.unique(key))
    if n_unique != table.num_rows:
        raise ValueError(
            f"(stock_id, date) is not unique: {table.num_rows} rows, {n_unique} distinct keys"
        )


def rebuild(
    dataset: str = "adj_daily_prices",
    tickers: Iterable[str] | None = None,
) -> dict[str, int | str]:
    """Rebuild every processed partition from the raw workbooks.

    Returns a summary dict suitable for a run manifest.
    """
    settings = get_settings()
    cfg = settings.dataset(dataset)
    raw_dir = settings.raw_dir_for(dataset)
    live_dir = settings.processed_dir_for(dataset)
    grain = cfg.partition

    table, errors = load_raw(raw_dir, tickers=tickers)
    if table.num_rows == 0:
        raise RuntimeError(f"no rows parsed from {raw_dir}; refusing to swap in an empty dataset")

    assert_unique_key(table)

    # Sort so partitions are contiguous, and so rows inside a file are ordered
    # by security — which makes the Parquet dictionary encoding of stock_id and
    # stock_name far more effective.
    table = table.sort_by([("date", "ascending"), ("stock_id", "ascending")])

    files = written = 0
    with StagedDirectory(live_dir) as staging:
        for rep_date, start, end in partition_runs(table, grain):
            chunk = table.slice(start, end - start)
            written += atomic_write_table(chunk, partition_path(staging, rep_date, grain))
            files += 1

    summary: dict[str, int | str] = {
        "dataset": dataset,
        "partition_grain": grain,
        "workbooks": len(raw_workbooks(raw_dir)) if tickers is None else len(list(tickers)),
        "securities": len(pc.unique(table.column("stock_id").combine_chunks())),
        "rows": written,
        "partitions": files,
        "date_min": str(pc.min(table.column("date")).as_py()),
        "date_max": str(pc.max(table.column("date")).as_py()),
        "read_errors": len(errors),
    }
    log.info(
        "rebuilt %s: %d rows in %d %s partitions (%s..%s), %d read errors",
        dataset,
        written,
        files,
        grain,
        summary["date_min"],
        summary["date_max"],
        len(errors),
    )
    return summary
