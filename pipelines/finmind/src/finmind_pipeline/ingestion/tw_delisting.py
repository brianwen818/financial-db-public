"""Ingest ``TaiwanStockDelisting`` — names and dates for delisted securities.

Measured 2026-08-30: 723 rows spanning 1995-09-23 to 2026-07-28, columns
``date`` / ``stock_id`` / ``stock_name``. 472 of those codes are absent from
the current security master.

**This dataset is not a complete delisting record.** Cross-checked against a
monthly whole-market sweep of 2004-2026, it names only 151 of the 441 delisted
ordinary shares the sweep found — and it misses recent ones too (``8115``
delisted 2023-10, ``2730`` 2024-03). So it is used for the names and dates it
does carry, never as the universe itself; see
``ingestion/universe_discovery.py`` for that.

``stock_name`` is occasionally a Chinese name followed by a quoted English one
containing commas ("錡電科,\"Auto Server Co., Ltd.\""), which is why anything
exporting it must quote properly.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from finmind_pipeline.processing.schema import RAW_DELISTING_SCHEMA, records_to_raw_table
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.finmind_client import FinMindClient
from finmind_pipeline.utils.parquet_io import atomic_write_table

log = logging.getLogger(__name__)

DATASET_KEY = "tw_delisting"


def raw_dir() -> Path:
    settings = get_settings()
    return settings.path(settings.dataset(DATASET_KEY).raw)


def ingest(client: FinMindClient, snapshot_date: dt.date | None = None) -> Path:
    settings = get_settings()
    dataset = settings.dataset(DATASET_KEY)
    snapshot_date = snapshot_date or dt.date.today()

    records = client.fetch(dataset.finmind_dataset)
    if not records:
        raise RuntimeError("TaiwanStockDelisting returned no rows")

    table = records_to_raw_table(records, RAW_DELISTING_SCHEMA)
    target = raw_dir() / f"{snapshot_date.isoformat()}.parquet"
    atomic_write_table(table, target)
    log.info(
        "tw_delisting: %d rows, %d distinct stock_id -> %s",
        table.num_rows,
        len(set(table.column("stock_id").to_pylist())),
        target.name,
    )
    return target


def latest_snapshot_path() -> Path | None:
    files = sorted(raw_dir().glob("*.parquet"))
    return files[-1] if files else None


def load_ids() -> set[str]:
    """Every delisted stock_id across all snapshots. Empty if never ingested."""
    import pyarrow.parquet as pq

    ids: set[str] = set()
    for path in sorted(raw_dir().glob("*.parquet")):
        table = pq.read_table(path, columns=["stock_id"])
        ids.update(x for x in table.column("stock_id").to_pylist() if x)
    return ids


def load_delisted_dates() -> dict[str, dt.date]:
    """``{stock_id: most recent delisting date}`` from the processed log.

    Latest date per code, because a security can delist, relist and delist
    again; only the most recent event describes its current state. Empty — not
    an error — before the dataset has been processed.
    """
    import pyarrow.parquet as pq

    settings = get_settings()
    path = settings.path(settings.dataset(DATASET_KEY).processed) / "tw_delisting.parquet"
    if not path.exists():
        return {}

    out: dict[str, dt.date] = {}
    table = pq.read_table(path, columns=["stock_id", "delisted_date"])
    for stock_id, date in zip(
        table.column("stock_id").to_pylist(),
        table.column("delisted_date").to_pylist(),
        strict=True,
    ):
        if not stock_id or date is None:
            continue
        if stock_id not in out or date > out[stock_id]:
            out[stock_id] = date
    return out
