"""Ingest ``TaiwanStockIndustryChain`` — supply-chain industry tagging.

Grain warning: this is many-to-many. A stock carries 1..61 rows (mean 2.9),
so neither ``stock_id`` nor ``(stock_id, date)`` is a key. The natural key is
``(stock_id, industry, sub_industry)``. ``date`` is a per-stock scrape stamp,
not an effective date.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from finmind_pipeline.processing.schema import RAW_INDUSTRY_SCHEMA, records_to_raw_table
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.finmind_client import FinMindClient
from finmind_pipeline.utils.parquet_io import atomic_write_table

log = logging.getLogger(__name__)

DATASET_KEY = "tw_industry"


def raw_dir() -> Path:
    settings = get_settings()
    return settings.path(settings.dataset(DATASET_KEY).raw)


def ingest(client: FinMindClient, snapshot_date: dt.date | None = None) -> Path:
    settings = get_settings()
    dataset = settings.dataset(DATASET_KEY)
    snapshot_date = snapshot_date or dt.date.today()

    records = client.fetch(dataset.finmind_dataset)
    if not records:
        raise RuntimeError("TaiwanStockIndustryChain returned no rows")

    table = records_to_raw_table(records, RAW_INDUSTRY_SCHEMA)
    target = raw_dir() / f"{snapshot_date.isoformat()}.parquet"
    atomic_write_table(table, target)
    log.info("tw_industry: %d rows -> %s", table.num_rows, target.name)
    return target


def latest_snapshot_path() -> Path | None:
    files = sorted(raw_dir().glob("*.parquet"))
    return files[-1] if files else None
