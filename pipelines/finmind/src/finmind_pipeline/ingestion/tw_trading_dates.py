"""Ingest ``TaiwanStockTradingDate`` — the official exchange calendar.

Covers 1999-01-05 onward only, and runs to the end of the current year (so it
contains future dates; never take a plain max() as "latest data day").

Earlier trading days are recovered in processing by unioning every date seen in
the price datasets. That pre-1999 segment includes Saturdays, because the
exchange traded on Saturdays until 1998.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from finmind_pipeline.processing.schema import (
    RAW_TRADING_DATE_SCHEMA,
    records_to_raw_table,
)
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.finmind_client import FinMindClient
from finmind_pipeline.utils.parquet_io import atomic_write_table

log = logging.getLogger(__name__)

DATASET_KEY = "tw_trading_dates"


def raw_dir() -> Path:
    settings = get_settings()
    return settings.path(settings.dataset(DATASET_KEY).raw)


def ingest(client: FinMindClient, snapshot_date: dt.date | None = None) -> Path:
    settings = get_settings()
    dataset = settings.dataset(DATASET_KEY)
    snapshot_date = snapshot_date or dt.date.today()

    records = client.fetch(dataset.finmind_dataset)
    if not records:
        raise RuntimeError("TaiwanStockTradingDate returned no rows")

    table = records_to_raw_table(records, RAW_TRADING_DATE_SCHEMA)
    target = raw_dir() / f"{snapshot_date.isoformat()}.parquet"
    atomic_write_table(table, target)
    log.info("tw_trading_dates: %d dates -> %s", table.num_rows, target.name)
    return target


def latest_snapshot_path() -> Path | None:
    files = sorted(raw_dir().glob("*.parquet"))
    return files[-1] if files else None
