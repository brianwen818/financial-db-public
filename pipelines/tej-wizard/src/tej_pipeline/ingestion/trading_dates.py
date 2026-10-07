"""Plan the missing trading-date rows that TEJ Smart Wizard needs as prompts.

The add-in only refreshes dates already present in the workbook.  Before a
refresh we therefore seed one row per missing Taiwan trading day, with the
identity in column A and the date in column B.  Smart Wizard then fills the
remaining fields when ``RefreshFile`` runs.

The two datasets need different plans:

* **Prices** are one workbook per security, most of which are still trading and
  some of which stopped years ago. The plan has to decide which workbooks are
  still active, and the horizon is *today* -- TEJ has no bar for a session that
  has not happened.
* **Index constituents** are one workbook per index-year, and only the current
  year moves. The horizon is the *next* session, because a constituent list
  dated D is published after D-1's close: on Friday evening TEJ already has
  Monday's. A horizon of today would leave that row permanently unfetched.
  Dates past today are seeded as *optional* -- if TEJ has not published yet,
  the prompt row is dropped rather than failing the run.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

from tej_pipeline.processing import index_schema as IS
from tej_pipeline.processing import schema as S
from tej_pipeline.processing.xlsx_reader import _INDEX_COL_INDEX, iter_rows


def load_trading_dates(path: Path, through: dt.date) -> tuple[dt.date, ...]:
    """Return unique calendar dates through ``through``, in ascending order."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"trading-date calendar not found: {path}")

    table = pq.read_table(path, columns=["date"])
    dates = table.column("date").combine_chunks()
    dates = pc.filter(dates, pc.less_equal(dates, through))
    return tuple(sorted(set(dates.to_pylist())))


def latest_workbook_date(path: Path, col_index: dict[str, int] | None = None) -> dt.date:
    """Read the newest row from a newest-first TEJ workbook.

    Column B is the date under both contracts, so this works for either; pass
    the index contract's column map to avoid allocating 35 slots per row.
    """
    rows = iter_rows(Path(path), col_index)
    try:
        next(rows)  # header
        for row in rows:
            if row[1] not in (None, ""):
                return S.excel_serial_to_date(row[1])
    finally:
        rows.close()
    raise ValueError(f"{Path(path).name}: no dated rows")


def missing_dates_for_workbooks(
    all_paths: list[Path],
    selected_paths: list[Path],
    trading_dates: tuple[dt.date, ...],
    active_window_sessions: int = 20,
) -> tuple[dict[str, tuple[dt.date, ...]], dt.date]:
    """Return missing dates for currently active workbooks.

    Historical constituents that stopped trading must not receive thousands of
    artificial rows.  The modal newest date is the stable market reference even
    after a partial test updates one or two files ahead of the rest.  Workbooks
    within ``active_window_sessions`` of that reference are treated as active.
    """
    latest = {path.stem: latest_workbook_date(path) for path in all_paths}
    if not latest:
        raise ValueError("no TEJ workbooks found")
    counts = Counter(latest.values())
    market_latest = max(counts, key=lambda date: (counts[date], date))
    recent_sessions = tuple(d for d in trading_dates if d <= market_latest)
    if not recent_sessions:
        raise ValueError(f"trading calendar does not cover raw latest date {market_latest}")
    active_cutoff = recent_sessions[-min(active_window_sessions, len(recent_sessions))]

    plans: dict[str, tuple[dt.date, ...]] = {}
    for path in selected_paths:
        workbook_latest = latest[path.stem]
        plans[path.stem] = (
            tuple(d for d in trading_dates if d > workbook_latest)
            if workbook_latest >= active_cutoff
            else ()
        )
    return plans, market_latest


def next_session(trading_dates: tuple[dt.date, ...], after: dt.date) -> dt.date | None:
    """The first trading date strictly after ``after``, or None past the calendar."""
    for date in trading_dates:
        if date > after:
            return date
    return None


@dataclass(frozen=True)
class IndexSeedPlan:
    """What to seed into one index's current-year workbook."""

    index_id: str
    path: Path
    workbook_latest: dt.date
    #: Settled sessions. TEJ must populate every one or the workbook is not saved.
    required: tuple[dt.date, ...]
    #: Sessions past ``as_of``. TEJ may not have published these yet; an unfilled
    #: row is dropped and the run still succeeds.
    optional: tuple[dt.date, ...]

    @property
    def seed_dates(self) -> tuple[dt.date, ...]:
        return tuple(sorted(self.required + self.optional))


def plan_index_seeds(
    workbooks: dict[str, Path],
    trading_dates: tuple[dt.date, ...],
    as_of: dt.date,
) -> list[IndexSeedPlan]:
    """Plan the seed rows for each index's current-year workbook.

    The horizon is the next trading session after ``as_of``, not ``as_of``
    itself, because this dataset leads the market by one session.

    Dates are filtered to the workbook's own year even when the horizon crosses
    into the next one. Seeding 2027-01-02 into ``2026.xlsx`` would put one
    session into two workbooks the moment ``2027.xlsx`` exists, which is exactly
    the overlap ``assert_years_do_not_overlap`` refuses to ingest. The new
    year's workbook has to be created first; nothing here invents it.
    """
    horizon = next_session(trading_dates, as_of) or as_of
    plans: list[IndexSeedPlan] = []
    for index_id, path in sorted(workbooks.items()):
        year = int(Path(path).stem)
        latest = latest_workbook_date(path, _INDEX_COL_INDEX)
        candidates = tuple(
            d for d in trading_dates if latest < d <= horizon and d.year == year
        )
        plans.append(
            IndexSeedPlan(
                index_id=index_id,
                path=Path(path),
                workbook_latest=latest,
                required=tuple(d for d in candidates if d <= as_of),
                optional=tuple(d for d in candidates if d > as_of),
            )
        )
    return plans


def index_identity(index_id: str) -> str:
    """The exact string TEJ writes into column A for an index.

    A seeded row carries it verbatim; a bare ``TWN50`` is not what the add-in
    resolves the query against.
    """
    try:
        return IS.INDEX_IDENTITIES[index_id]
    except KeyError:
        known = ", ".join(sorted(IS.INDEX_IDENTITIES))
        raise KeyError(f"unknown index {index_id!r}; known: {known}") from None
