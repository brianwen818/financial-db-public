"""Trading-calendar loading and gap arithmetic.

Two sources feed the calendar:

* ``TaiwanStockTradingDate`` — authoritative, but only from 1999-01-05 onward.
* The union of every date observed in the price datasets — extends coverage
  back to 1994-10-01.

The expanded calendar is the union of both. Note that the pre-1999 segment
contains **Saturdays**: the Taiwan exchange traded on Saturdays until 1998, so
roughly 1,204 extra days appear there — more than the weekday count for the
period. A weekday-only assumption anywhere in this pipeline is a bug.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pyarrow.parquet as pq

log = logging.getLogger(__name__)

DateLike = dt.date | str


def to_date(value: DateLike) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def load_calendar(path: Path) -> list[dt.date]:
    """Read the expanded trading calendar Parquet into a sorted date list."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"trading calendar not built yet: {path}. "
            "Run the tw_trading_dates ingestion and expanded_trading_dates processing first."
        )
    table = pq.read_table(path, columns=["date"])
    dates = table.column("date").to_pylist()
    return sorted({to_date(d) for d in dates if d is not None})


def trading_days_between(
    calendar: list[dt.date], start: DateLike | None, end: DateLike | None
) -> list[dt.date]:
    lo = to_date(start) if start else dt.date.min
    hi = to_date(end) if end else dt.date.max
    return [d for d in calendar if lo <= d <= hi]


def last_closed_trading_day(
    calendar: list[dt.date], today: dt.date | None = None
) -> dt.date | None:
    """Most recent calendar date that is not in the future.

    The calendar Parquet extends into the future (FinMind publishes the full
    year), so a plain ``max()`` would return December 31st.
    """
    today = today or dt.date.today()
    past = [d for d in calendar if d <= today]
    return past[-1] if past else None


def missing_dates(
    calendar: list[dt.date],
    present: set[dt.date] | set[str],
    start: DateLike | None = None,
    end: DateLike | None = None,
) -> list[dt.date]:
    """Calendar days in ``[start, end]`` that are absent from ``present``.

    This is the whole of reconciliation step A: because the whole-market pull
    is authoritative for a given day, a day is either fully present or missing.
    """
    have = {to_date(d) for d in present}
    return [d for d in trading_days_between(calendar, start, end) if d not in have]


def shift_trading_days(calendar: list[dt.date], anchor: dt.date, offset: int) -> dt.date:
    """Move ``offset`` trading days from ``anchor`` (negative moves backwards)."""
    if not calendar:
        raise ValueError("empty calendar")
    # bisect over a sorted list; anchor need not itself be a trading day
    import bisect

    idx = bisect.bisect_left(calendar, anchor)
    target = min(max(idx + offset, 0), len(calendar) - 1)
    return calendar[target]
