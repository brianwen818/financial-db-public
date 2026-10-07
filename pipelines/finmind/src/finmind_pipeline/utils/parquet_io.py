"""Parquet writing and reading with atomic semantics.

Every write goes to a temporary sibling file first and is then moved into place
with ``os.replace``, which is atomic on Windows. A killed process therefore
never leaves a half-written Parquet file that a later DuckDB scan would choke
on — the target either has the old content or the new content, never a mix.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from finmind_pipeline.utils.fsutil import rename_with_retry, replace_with_retry

log = logging.getLogger(__name__)

COMPRESSION = "zstd"
COMPRESSION_LEVEL = 3

# Names Windows refuses to create regardless of extension.
_WIN_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(stock_id: str) -> str:
    """Turn a stock_id into a filesystem-safe stem, injectively.

    FinMind stock_ids are not all numeric — they run from ``0050`` through
    ``00679B`` to ``TradingConsumersGoods``. In practice none of them need
    escaping, but this must never map two distinct securities onto one file, so
    anything that does need altering gets a short digest of the original
    appended. Plain escaping would collide (``CON`` and ``_CON`` both becoming
    ``_CON``) and silently merge two tickers' histories.
    """
    stem = _ILLEGAL_CHARS.sub("_", stock_id).rstrip(". ")
    if not stem:
        raise ValueError(f"stock_id {stock_id!r} maps to an empty filename")
    if stem != stock_id or stem.upper() in _WIN_RESERVED:
        digest = hashlib.sha1(stock_id.encode("utf-8")).hexdigest()[:8]
        stem = f"_{stem}_{digest}"
    return stem


def day_partition_path(root: Path, date: dt.date | str) -> Path:
    """``root/year=YYYY/month=MM/YYYY-MM-DD.parquet``.

    Keeping one file per trading day makes the daily append a pure file
    creation — idempotent, and an interrupted run cannot corrupt earlier days.
    """
    if isinstance(date, str):
        date = dt.date.fromisoformat(date)
    return (
        root / f"year={date.year:04d}" / f"month={date.month:02d}" / f"{date.isoformat()}.parquet"
    )


def atomic_write_table(table: pa.Table, path: Path) -> int:
    """Write ``table`` to ``path`` atomically. Returns the row count written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        pq.write_table(
            table,
            tmp,
            compression=COMPRESSION,
            compression_level=COMPRESSION_LEVEL,
            use_dictionary=True,
            version="2.6",
        )
        replace_with_retry(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return table.num_rows


def read_table(path: Path, columns: list[str] | None = None) -> pa.Table:
    return pq.read_table(path, columns=columns)


def read_many(paths: list[Path], columns: list[str] | None = None) -> pa.Table:
    """Concatenate several Parquet files, skipping any that are missing."""
    tables = [pq.read_table(p, columns=columns) for p in paths if Path(p).exists()]
    if not tables:
        return pa.table({})
    return pa.concat_tables(tables, promote_options="default")


def row_count(path: Path) -> int:
    """Row count from the Parquet footer — no data pages are read."""
    return pq.ParquetFile(path).metadata.num_rows


class StagedDirectory:
    """Build a dataset directory off to the side, then swap it in.

    Used by the weekly adjusted-price rebuild, which regenerates every daily
    partition from scratch. Writing in place would leave the dataset in a
    half-rebuilt state for minutes; this keeps the live directory intact until
    the replacement is complete.

    The swap itself is a delete-then-rename, so there is a sub-second window
    where the live path does not exist. That is logged, and the previous
    contents are kept until the rename succeeds.
    """

    def __init__(self, live: Path) -> None:
        self.live = Path(live)
        self.staging = self.live.with_name(self.live.name + "__staging")
        self.backup = self.live.with_name(self.live.name + "__old")

    def __enter__(self) -> Path:
        if self.staging.exists():
            shutil.rmtree(self.staging)
        self.staging.mkdir(parents=True)
        return self.staging

    def __exit__(self, exc_type: type | None, *_: object) -> None:
        if exc_type is not None:
            log.error("staged build of %s failed; live data untouched", self.live)
            shutil.rmtree(self.staging, ignore_errors=True)
            return

        if self.backup.exists():
            shutil.rmtree(self.backup)
        log.info("swapping staged build into %s", self.live)
        if self.live.exists():
            rename_with_retry(self.live, self.backup)
        try:
            rename_with_retry(self.staging, self.live)
        except BaseException:
            # Put the original back rather than leaving nothing in place.
            if self.backup.exists() and not self.live.exists():
                rename_with_retry(self.backup, self.live)
                log.error("swap failed; restored previous %s", self.live)
            raise
        shutil.rmtree(self.backup, ignore_errors=True)
        log.info("swap complete: %s", self.live)


def dataset_size_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in Path(root).rglob("*.parquet"))


def dataset_file_count(root: Path) -> int:
    return sum(1 for _ in Path(root).rglob("*.parquet"))
