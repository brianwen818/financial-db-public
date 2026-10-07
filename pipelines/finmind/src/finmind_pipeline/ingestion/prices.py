"""Shared price ingestion for ``TaiwanStockPrice`` and ``TaiwanStockPriceAdj``.

Both datasets have an identical shape, so one implementation serves both;
``daily_prices`` and ``adj_daily_prices`` are thin wrappers over it.

Two fetch units exist, and they land in separate raw directories because their
shapes differ:

``market``
    One call returns the whole market for a single day. Used by the daily job.
    ``end_date`` is ignored by the API when ``data_id`` is omitted, so this is
    strictly one day per call. The raw response includes ~40,900 warrants and
    TDRs that are out of scope, so it is filtered to the security-master
    universe before landing.

``ticker``
    One call returns a ticker's entire history — no row cap. Used by the
    backfill and by the weekly adjusted-price rebuild.

Raw files are never mutated: the market directory only gains new dates, and the
ticker directory is rewritten wholesale per ticker (atomically) when refreshed.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from finmind_pipeline.processing.schema import RAW_PRICE_SCHEMA, records_to_raw_table
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.finmind_client import FinMindClient
from finmind_pipeline.utils.parquet_io import atomic_write_table, safe_filename

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchResult:
    rows_returned: int
    rows_written: int
    path: Path | None
    skipped: bool = False


def _market_dir(dataset_key: str) -> Path:
    settings = get_settings()
    return settings.path(settings.dataset(dataset_key).raw_market)


def _ticker_dir(dataset_key: str) -> Path:
    settings = get_settings()
    return settings.path(settings.dataset(dataset_key).raw_ticker)


def market_day_path(dataset_key: str, date: dt.date | str) -> Path:
    if isinstance(date, str):
        date = dt.date.fromisoformat(date)
    return _market_dir(dataset_key) / f"date={date.isoformat()}.parquet"


def ticker_path(dataset_key: str, stock_id: str) -> Path:
    return _ticker_dir(dataset_key) / f"{safe_filename(stock_id)}.parquet"


# ---------------------------------------------------------------------------


def fetch_market_day(
    client: FinMindClient,
    dataset_key: str,
    date: dt.date | str,
    universe: set[str] | None = None,
    overwrite: bool = False,
) -> FetchResult:
    """Fetch and land one whole-market day.

    Returns ``skipped=True`` when the target already exists and ``overwrite``
    is False, which makes the daily job safely re-runnable.
    """
    settings = get_settings()
    dataset = settings.dataset(dataset_key)
    if isinstance(date, str):
        date = dt.date.fromisoformat(date)
    target = market_day_path(dataset_key, date)

    if target.exists() and not overwrite:
        return FetchResult(0, 0, target, skipped=True)

    records = client.fetch_market_day(dataset.finmind_dataset, date.isoformat())
    returned = len(records)
    if not records:
        # Non-trading day, or the day has not been published yet.
        log.info("%s %s: no data (non-trading day?)", dataset_key, date)
        return FetchResult(0, 0, None)

    if dataset.filter_to_universe and universe is not None:
        records = [r for r in records if r.get("stock_id") in universe]
        log.info(
            "%s %s: %d returned, %d in universe (%d filtered out)",
            dataset_key,
            date,
            returned,
            len(records),
            returned - len(records),
        )
        if not records:
            return FetchResult(returned, 0, None)

    table = records_to_raw_table(records, RAW_PRICE_SCHEMA)
    atomic_write_table(table, target)
    return FetchResult(returned, table.num_rows, target)


def fetch_ticker(
    client: FinMindClient,
    dataset_key: str,
    stock_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    overwrite: bool = True,
) -> FetchResult:
    """Fetch and land one ticker's full history.

    An empty response is normal — a delisted code, or one that never traded
    under the requested dataset — and writes no file.
    """
    settings = get_settings()
    dataset = settings.dataset(dataset_key)
    target = ticker_path(dataset_key, stock_id)

    if target.exists() and not overwrite:
        return FetchResult(0, 0, target, skipped=True)

    records = client.fetch_ticker_history(
        dataset.finmind_dataset,
        stock_id,
        start_date=start_date or settings.history_start,
        end_date=end_date,
    )
    if not records:
        return FetchResult(0, 0, None)

    table = records_to_raw_table(records, RAW_PRICE_SCHEMA)
    atomic_write_table(table, target)
    return FetchResult(len(records), table.num_rows, target)


def fetch_tickers(
    client: FinMindClient,
    dataset_key: str,
    stock_ids: Iterable[str],
    *,
    overwrite: bool = True,
    progress_every: int = 100,
    on_progress: object = None,
) -> dict[str, int]:
    """Fetch many tickers sequentially. Returns ``{stock_id: rows_written}``.

    Rate limiting is handled inside the client, so this simply iterates. A
    ticker that fails is logged and skipped rather than aborting the batch —
    the reconciliation step will pick it up on the next weekly run.
    """
    written: dict[str, int] = {}
    ids = list(stock_ids)
    total = len(ids)
    for i, stock_id in enumerate(ids, start=1):
        try:
            result = fetch_ticker(client, dataset_key, stock_id, overwrite=overwrite)
            written[stock_id] = result.rows_written
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not stop 3,000
            log.warning("%s %s failed: %s", dataset_key, stock_id, exc)
            written[stock_id] = -1
        if callable(on_progress):
            on_progress(i, total, stock_id)
        if progress_every and i % progress_every == 0:
            ok = sum(1 for v in written.values() if v > 0)
            log.info(
                "%s: %d/%d fetched (%d with data, %d rows total)",
                dataset_key,
                i,
                total,
                ok,
                sum(v for v in written.values() if v > 0),
            )
    return written


def existing_market_dates(dataset_key: str) -> set[dt.date]:
    """Dates already present in the raw market directory."""
    out: set[dt.date] = set()
    for path in _market_dir(dataset_key).glob("date=*.parquet"):
        try:
            out.add(dt.date.fromisoformat(path.stem.split("=", 1)[1]))
        except (IndexError, ValueError):
            log.warning("unparseable raw market filename: %s", path.name)
    return out


def existing_tickers(dataset_key: str) -> set[str]:
    return {p.stem for p in _ticker_dir(dataset_key).glob("*.parquet")}
