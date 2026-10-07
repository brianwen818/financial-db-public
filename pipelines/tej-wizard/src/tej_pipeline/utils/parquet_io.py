"""Parquet writing and reading with atomic semantics.

Every write goes to a temporary sibling file first and is then moved into place
with ``os.replace``, which is atomic on Windows. A killed process therefore
never leaves a half-written Parquet file that a later DuckDB scan would choke
on — the target either has the old content or the new content, never a mix.
"""

from __future__ import annotations

import datetime as dt
import logging
import shutil
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tej_pipeline.utils.fsutil import rename_with_retry, replace_with_retry

log = logging.getLogger(__name__)

COMPRESSION = "zstd"
COMPRESSION_LEVEL = 3

PARTITION_GRAINS = ("day", "month")


def partition_path(root: Path, date: dt.date | str, grain: str = "day") -> Path:
    """Hive-partitioned path for one date.

    ``day``   -> ``root/year=YYYY/month=MM/YYYY-MM-DD.parquet``
    ``month`` -> ``root/year=YYYY/month=MM/YYYY-MM.parquet``

    Both keep ``year`` and ``month`` as the Hive keys, so the same partition
    pruning rule applies either way: DuckDB cannot infer them from a ``date``
    predicate, and a query must constrain ``year`` to prune whole directories.
    The ``adj_daily_prices_between()`` macro does this for you.

    ``day`` mirrors the FinMind layout exactly. ``month`` exists because this
    dataset holds only ~102 rows per trading day, so per-day files would be
    ~6,400 files of a few KB each; the update unit here is the ticker, never
    the day, so per-day files buy none of the append idempotency they buy in
    the FinMind pipeline.
    """
    if isinstance(date, str):
        date = dt.date.fromisoformat(date)
    directory = root / f"year={date.year:04d}" / f"month={date.month:02d}"
    if grain == "day":
        return directory / f"{date.isoformat()}.parquet"
    if grain == "month":
        return directory / f"{date.year:04d}-{date.month:02d}.parquet"
    raise ValueError(f"unknown partition grain {grain!r}; expected one of {PARTITION_GRAINS}")


def partition_runs(
    table: pa.Table, grain: str, date_column: str = "date"
) -> Iterator[tuple[dt.date, int, int]]:
    """Yield ``(representative_date, start, end)`` for each partition.

    The table must already be sorted by date, which makes every partition a
    contiguous slice. Filtering per partition instead would be O(rows x
    partitions) -- 4 billion comparisons at day grain on the price dataset --
    and is why this walks run boundaries rather than calling ``filter`` 6,400
    times.
    """
    dates = table.column(date_column).to_pylist()
    if not dates:
        return

    def key(d: dt.date) -> tuple[int, ...]:
        return (d.year, d.month) if grain == "month" else (d.year, d.month, d.day)

    start = 0
    current = key(dates[0])
    for i in range(1, len(dates)):
        k = key(dates[i])
        if k != current:
            yield dates[start], start, i
            start, current = i, k
    yield dates[start], start, len(dates)


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


def row_count(path: Path) -> int:
    """Row count from the Parquet footer — no data pages are read."""
    return pq.ParquetFile(path).metadata.num_rows


class StagedDirectory:
    """Build a dataset directory off to the side, then swap it in.

    Every rebuild here regenerates all partitions, because the refresh unit is
    a ticker's whole history and one refreshed workbook touches every date it
    covers. Writing in place would leave the dataset half-rebuilt for the
    minutes the parse takes; this keeps the live directory intact until the
    replacement is complete.

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
