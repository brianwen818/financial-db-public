"""Ingest ``TaiwanStockInfo`` — the security master.

This dataset is a cumulative, append-only snapshot log rather than a current
state table: 4,321 processed rows covering 3,147 distinct securities across 263
scrape dates, because ``industry_category`` gets renamed upstream over time.
Delisted securities stay in it, which is exactly what a backfill universe needs.

One raw snapshot lands per ingest date and they all stay. Processing collapses
them on content -- upstream re-stamps most rows with the crawl date, so the
processed log would otherwise grow by ~3,300 rows a run.

It also doubles as the filter for the whole-market price pull, which returns
~43,700 securities per day — the extra ~40,900 being warrants and TDRs that are
deliberately out of scope.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pyarrow.parquet as pq

from finmind_pipeline.processing.schema import (
    RAW_STOCK_INFO_SCHEMA,
    records_to_raw_table,
)
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.finmind_client import FinMindClient
from finmind_pipeline.utils.parquet_io import atomic_write_table

log = logging.getLogger(__name__)

DATASET_KEY = "tw_stock_info"


def raw_dir() -> Path:
    settings = get_settings()
    return settings.path(settings.dataset(DATASET_KEY).raw)


def ingest(client: FinMindClient, snapshot_date: dt.date | None = None) -> Path:
    """Fetch the full security master and land one snapshot Parquet."""
    settings = get_settings()
    dataset = settings.dataset(DATASET_KEY)
    snapshot_date = snapshot_date or dt.date.today()

    records = client.fetch(dataset.finmind_dataset)
    if not records:
        raise RuntimeError("TaiwanStockInfo returned no rows")

    table = records_to_raw_table(records, RAW_STOCK_INFO_SCHEMA)
    target = raw_dir() / f"{snapshot_date.isoformat()}.parquet"
    atomic_write_table(table, target)
    log.info(
        "tw_stock_info: %d rows, %d distinct stock_id -> %s",
        table.num_rows,
        len(set(table.column("stock_id").to_pylist())),
        target.name,
    )
    return target


def latest_snapshot_path() -> Path | None:
    files = sorted(raw_dir().glob("*.parquet"))
    return files[-1] if files else None


def load_current_universe(include_index: bool = True) -> set[str]:
    """Only what the newest snapshot lists — i.e. what is listed *today*.

    The union in :func:`load_universe` is right for a backfill and wrong for
    anything that re-fetches on a schedule. Once a security delists it stops
    appearing in new snapshots but stays in the union forever, and its last bar
    stops moving; a staleness check over the union would therefore flag it every
    week for the rest of the project's life and re-fetch history that cannot
    change. Checks that ask "should this have fresh data?" belong here.
    """
    settings = get_settings()
    path = latest_snapshot_path()
    if path is None:
        raise FileNotFoundError(f"no TaiwanStockInfo snapshots in {raw_dir()}; run ingestion first")

    table = pq.read_table(path, columns=["stock_id", "industry_category"])
    universe: set[str] = set()
    for stock_id, category in zip(
        table.column("stock_id").to_pylist(),
        table.column("industry_category").to_pylist(),
        strict=True,
    ):
        if not stock_id:
            continue
        if not include_index and settings.is_index_category(category):
            continue
        universe.add(stock_id)
    log.info("current universe: %d ids from %s", len(universe), path.name)
    return universe


def load_delisted_universe() -> set[str]:
    """Securities that traded but are absent from the master.

    Read from the processed ``delisted_securities`` table, which is already
    restricted to in-scope shapes. Empty — never an error — when no discovery
    sweep has run yet.
    """
    settings = get_settings()
    path = settings.path(settings.dataset("discovered_universe").processed) / (
        "delisted_securities.parquet"
    )
    if not path.exists():
        return set()
    column = pq.read_table(path, columns=["stock_id"]).column("stock_id").to_pylist()
    return {s for s in column if s}


def load_universe(include_index: bool = True, include_delisted: bool = False) -> set[str]:
    """Every stock_id ever seen in the security master.

    The union across *all* snapshots, not just the newest, so that securities
    delisted from here on stay in the universe. That only protects the future:
    this project's first snapshot is 2026-08-26, so anything that delisted
    earlier was never recorded and needs ``include_delisted``.

    ``include_index`` keeps the 32 index pseudo-tickers (``TAIEX``, ``TPEx``,
    the 30 sector indices). They carry genuine price history — ``TAIEX`` has
    6,849 rows back to 1999 — so they are in scope by default and are flagged
    with ``is_index`` in the processed layer rather than dropped.

    ``include_delisted`` adds the securities recovered by the discovery sweep.
    It defaults to False because the weekly job uses this set both to re-fetch
    adjusted prices and to flag stale tickers: a delisted security's history is
    immutable and its last bar is years old, so including it there would mean
    hundreds of pointless calls a week and a permanent list of false staleness.
    The backfill, which wants everything that ever traded, passes True.
    """
    settings = get_settings()
    files = sorted(raw_dir().glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no TaiwanStockInfo snapshots in {raw_dir()}; run ingestion first")

    universe: set[str] = set()
    for path in files:
        table = pq.read_table(path, columns=["stock_id", "industry_category"])
        for stock_id, category in zip(
            table.column("stock_id").to_pylist(),
            table.column("industry_category").to_pylist(),
            strict=True,
        ):
            if not stock_id:
                continue
            if not include_index and settings.is_index_category(category):
                continue
            universe.add(stock_id)

    listed = len(universe)
    delisted = 0
    if include_delisted:
        extra = load_delisted_universe() - universe
        delisted = len(extra)
        universe |= extra
    log.info(
        "ticker universe: %d ids from %d snapshots (+%d delisted)",
        listed,
        len(files),
        delisted,
    )
    return universe
