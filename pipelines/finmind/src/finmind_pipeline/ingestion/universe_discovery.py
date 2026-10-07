"""Find securities that traded but are absent from the security master.

``TaiwanStockInfo`` is a current-state list. A company that delisted before the
first snapshot was captured (2026-08-26 here) never appears in it, so the
per-ticker backfill — which iterates the master — never asks for it, and the
warehouse ends up holding survivors only. That is survivorship bias, and on a
2010 cross-section it hides roughly 9% of the ordinary shares that actually
traded.

The whole-market endpoint does not have this problem: asked for a historical
day, it returns whatever traded *that day*, delisted or not. Sweeping it is
therefore the only way to recover the true historical universe.

**Sampling, not exhaustion.** A day-by-day sweep of 2004-02-11 onward is 5,551
calls per dataset. Sampling one trading day per month is 271 calls and finds
441 delisted ordinary shares; a coarser sweep of one day per *year* (23 calls)
finds 425 of the same 441. Twelvefold more calls bought 16 extra codes, only
three of which lived under two months. The curve has flattened, so monthly is
where this stops. Anything shorter-lived than a month is below the noise floor
of a backtest.

The sweep records every code it sees, including the ~135,000 warrants and TDRs
that are out of scope, together with its shape. Scope is then a filter applied
at load time (:data:`IN_SCOPE_SHAPES`), which means widening scope later is a
config change rather than another 271 calls.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from finmind_pipeline.processing.schema import RAW_DISCOVERED_UNIVERSE_SCHEMA
from finmind_pipeline.processing.universe import IN_SCOPE_SHAPES, classify_stock_id
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.finmind_client import FinMindClient
from finmind_pipeline.utils.parquet_io import atomic_write_table

log = logging.getLogger(__name__)

DATASET_KEY = "discovered_universe"


def raw_dir() -> Path:
    settings = get_settings()
    return settings.path(settings.dataset(DATASET_KEY).raw)


def history_start() -> dt.date:
    settings = get_settings()
    ds = settings.dataset(DATASET_KEY)
    return dt.date.fromisoformat(ds.history_start or settings.history_start)


@dataclass
class Observation:
    stock_id: str
    hits: int = 0
    first_seen: dt.date | None = None
    last_seen: dt.date | None = None

    def record(self, date: dt.date) -> None:
        self.hits += 1
        if self.first_seen is None or date < self.first_seen:
            self.first_seen = date
        if self.last_seen is None or date > self.last_seen:
            self.last_seen = date


@dataclass
class SweepResult:
    observations: dict[str, Observation] = field(default_factory=dict)
    days_sampled: list[dt.date] = field(default_factory=list)
    days_empty: list[dt.date] = field(default_factory=list)
    api_calls: int = 0


# ---------------------------------------------------------------------------


def monthly_sample_days(
    calendar: list[dt.date],
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> list[dt.date]:
    """One trading day per calendar month — the middle one.

    Mid-month avoids both the month-end settlement bulge and the long Lunar New
    Year gap, either of which would bias which securities are observed.
    """
    start = start or history_start()
    by_month: dict[tuple[int, int], list[dt.date]] = defaultdict(list)
    for d in calendar:
        if d < start or (end and d > end):
            continue
        by_month[(d.year, d.month)].append(d)
    return [days[(len(days) - 1) // 2] for _, days in sorted(by_month.items()) if days]


def sweep(
    client: FinMindClient,
    days: list[dt.date],
    on_progress: object = None,
) -> SweepResult:
    """Call the whole-market endpoint for each day and tally every code seen."""
    settings = get_settings()
    dataset = settings.dataset(DATASET_KEY).finmind_dataset
    result = SweepResult()
    started = client.call_count

    for i, day in enumerate(days, start=1):
        records = client.fetch_market_day(dataset, day.isoformat())
        if not records:
            # A market holiday the published calendar got wrong, or a day the
            # API has no data for. Neither is an error.
            result.days_empty.append(day)
        else:
            result.days_sampled.append(day)
            for rec in records:
                stock_id = rec.get("stock_id")
                if not stock_id:
                    continue
                obs = result.observations.get(stock_id)
                if obs is None:
                    obs = result.observations[stock_id] = Observation(stock_id)
                obs.record(day)
        if callable(on_progress):
            on_progress(i, len(days), day, len(result.observations))

    result.api_calls = client.call_count - started
    return result


def to_table(result: SweepResult, discovered_at: dt.datetime | None = None) -> pa.Table:
    discovered_at = discovered_at or dt.datetime.now(dt.UTC)
    n_days = len(result.days_sampled)
    rows = sorted(result.observations.values(), key=lambda o: o.stock_id)
    return pa.table(
        {
            "stock_id": [o.stock_id for o in rows],
            "shape": [classify_stock_id(o.stock_id) for o in rows],
            "first_seen": [o.first_seen for o in rows],
            "last_seen": [o.last_seen for o in rows],
            "sample_hits": [o.hits for o in rows],
            "sampled_days": [n_days] * len(rows),
            "discovered_at": [discovered_at] * len(rows),
        },
        schema=RAW_DISCOVERED_UNIVERSE_SCHEMA,
    )


def ingest(
    client: FinMindClient,
    calendar: list[dt.date],
    snapshot_date: dt.date | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    on_progress: object = None,
) -> tuple[Path, SweepResult]:
    """Run a monthly sweep and land one discovery snapshot."""
    snapshot_date = snapshot_date or dt.date.today()
    days = monthly_sample_days(calendar, start, end)
    if not days:
        raise RuntimeError("no trading days to sample; is the calendar built?")

    log.info(
        "universe discovery: sampling %d months, %s .. %s",
        len(days),
        days[0],
        days[-1],
    )
    result = sweep(client, days, on_progress)

    table = to_table(result)
    target = raw_dir() / f"{snapshot_date.isoformat()}.parquet"
    atomic_write_table(table, target)

    shapes: dict[str, int] = defaultdict(int)
    for shape in table.column("shape").to_pylist():
        shapes[shape] += 1
    log.info(
        "universe discovery: %d days sampled (%d empty), %d codes seen, shapes %s -> %s",
        len(result.days_sampled),
        len(result.days_empty),
        table.num_rows,
        dict(sorted(shapes.items(), key=lambda kv: -kv[1])),
        target.name,
    )
    return target, result


def load_discovered(shapes: frozenset[str] | None = None) -> dict[str, dict]:
    """Merge every discovery snapshot into ``{stock_id: {...}}``.

    Later snapshots extend earlier ones rather than replacing them: a sweep run
    with a narrower date range must not shrink what an earlier wide sweep saw.
    """
    shapes = IN_SCOPE_SHAPES if shapes is None else shapes
    merged: dict[str, dict] = {}
    for path in sorted(raw_dir().glob("*.parquet")):
        table = pq.read_table(path)
        for row in table.to_pylist():
            stock_id = row["stock_id"]
            if not stock_id or row["shape"] not in shapes:
                continue
            prev = merged.get(stock_id)
            if prev is None:
                merged[stock_id] = dict(row)
                continue
            prev["sample_hits"] += row["sample_hits"]
            for key, better in (("first_seen", min), ("last_seen", max)):
                if row[key] is not None:
                    prev[key] = better(prev[key], row[key]) if prev[key] else row[key]
    return merged


def load_discovered_ids(shapes: frozenset[str] | None = None) -> set[str]:
    return set(load_discovered(shapes))
