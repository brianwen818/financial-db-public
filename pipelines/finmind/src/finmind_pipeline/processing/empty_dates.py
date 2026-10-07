"""Dates the exchange calendar claims, but on which nothing actually traded.

``TaiwanStockTradingDate`` is not perfectly accurate. 2026-07-10 is listed as a
trading day, yet the whole-market endpoint returns zero securities for it — a
market closure (typhoon days are the usual cause) that never made it back into
the published calendar.

Without a record of that, reconciliation reports it as a gap forever and
re-fetches it on every weekly run. Confirming it once and remembering the answer
keeps the gap report meaningful: anything still listed there is a genuine
problem.

The safety rule is that a date is only recorded once it is **strictly older than
the newest date we already hold**. An empty response for today, or for a day
FinMind has not published yet, means "not ready", not "nothing traded" — and
recording those would permanently blind the pipeline to real gaps.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.parquet_io import atomic_write_table

log = logging.getLogger(__name__)

SCHEMA = pa.schema(
    [
        ("date", pa.date32()),
        ("confirmed_at", pa.timestamp("us", tz="UTC")),
    ]
)

DIRNAME = "known-empty-dates"
FILENAME = "known_empty_dates.parquet"


def path() -> Path:
    return get_settings().processed_dir / DIRNAME / FILENAME


def load() -> set[dt.date]:
    p = path()
    if not p.exists():
        return set()
    return {d for d in pq.read_table(p, columns=["date"]).column("date").to_pylist() if d}


def record(
    dates: Iterable[dt.date],
    newest_held: dt.date | None,
    confirmed_at: dt.datetime | None = None,
) -> set[dt.date]:
    """Add confirmed-empty dates. Returns the full set after the update.

    ``newest_held`` is the latest date for which data exists. Anything at or
    after it is treated as "not published yet" and is deliberately ignored.
    """
    candidates = {d for d in dates if d is not None}
    if newest_held is not None:
        skipped = {d for d in candidates if d >= newest_held}
        if skipped:
            log.info(
                "not recording %d empty date(s) at or after the newest held date %s "
                "(unpublished, not closed): %s",
                len(skipped),
                newest_held,
                sorted(skipped)[:5],
            )
        candidates -= skipped

    existing = load()
    fresh = candidates - existing
    if not fresh:
        return existing

    confirmed_at = confirmed_at or dt.datetime.now(dt.UTC)
    combined = sorted(existing | fresh)
    table = pa.table(
        {
            "date": combined,
            "confirmed_at": [confirmed_at] * len(combined),
        },
        schema=SCHEMA,
    )
    atomic_write_table(table, path())
    log.info(
        "recorded %d confirmed-empty trading date(s): %s",
        len(fresh),
        sorted(fresh)[:10],
    )
    return set(combined)


def ensure_exists() -> Path:
    """Create an empty file so the DuckDB view always has something to read."""
    p = path()
    if not p.exists():
        atomic_write_table(pa.table({"date": [], "confirmed_at": []}, schema=SCHEMA), p)
    return p
