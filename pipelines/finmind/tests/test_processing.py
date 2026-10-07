"""Processing-layer tests: dedup priority, calendar expansion, and the
value-for-value contract against the golden Feather samples.

Tests that need a populated warehouse skip when it is absent, so the suite
still runs on a fresh checkout.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pyarrow as pa
import pytest

from finmind_pipeline.processing.prices import _SELECT, _finalise, _source_rank
from finmind_pipeline.processing.schema import PROCESSED_PRICE_SCHEMA
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.trading_calendar import (
    last_closed_trading_day,
    missing_dates,
    shift_trading_days,
    to_date,
    trading_days_between,
)


def _raw_row(stock_id="0050", date="2026-08-21", close=100.0, volume=1000):
    return {
        "date": date,
        "stock_id": stock_id,
        "Trading_Volume": volume,
        "Trading_money": 999,
        "open": 1.0,
        "max": 2.0,
        "min": 0.5,
        "close": close,
        "spread": 0.1,
        "Trading_turnover": 7,
    }


class TestPriceTransform:
    """The SELECT that turns a raw response into the processed contract."""

    def _run(self, rows, source="market"):
        con = duckdb.connect()
        try:
            con.register("raw", pa.Table.from_pylist(rows))
            return con.execute(f"SELECT {_SELECT}, '{source}' AS source FROM raw").to_arrow_table()
        finally:
            con.close()

    def test_renames_max_min_to_high_low(self):
        out = self._run([_raw_row()])
        assert "high" in out.column_names and "low" in out.column_names
        assert "max" not in out.column_names and "min" not in out.column_names
        assert out.column("high").to_pylist() == [2.0]
        assert out.column("low").to_pylist() == [0.5]

    def test_date_becomes_real_date_type(self):
        out = self._run([_raw_row()])
        assert out.schema.field("date").type == pa.date32()
        assert out.column("date").to_pylist() == [dt.date(2026, 8, 21)]

    def test_stock_id_stays_text_with_leading_zeros(self):
        out = self._run([_raw_row(stock_id="0050")])
        assert out.column("stock_id").to_pylist() == ["0050"]

    def test_finalise_matches_processed_schema_exactly(self):
        out = _finalise(self._run([_raw_row()]), dt.datetime.now(dt.UTC))
        assert out.schema == PROCESSED_PRICE_SCHEMA


class TestDedupPriority:
    """Which fetch unit wins when both cover a day depends on the dataset.

    Unadjusted: the market pull, the settled end-of-day snapshot (a ticker file
    may have been fetched mid-session). Adjusted: the ticker file, because a
    market-day file is a snapshot of an older adjustment basis.
    """

    def _dedup(self, ticker_rows, market_rows, prefer="market"):
        con = duckdb.connect()
        try:
            con.register("t_raw", pa.Table.from_pylist(ticker_rows))
            con.register("m_raw", pa.Table.from_pylist(market_rows))
            return con.execute(f"""
                WITH combined AS (
                    SELECT {_SELECT}, 'ticker' AS source FROM t_raw
                    UNION ALL
                    SELECT {_SELECT}, 'market' AS source FROM m_raw
                ),
                ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY stock_id, date
                        ORDER BY {_source_rank(prefer)}
                    ) AS rn
                    FROM combined WHERE date IS NOT NULL AND stock_id IS NOT NULL
                )
                SELECT * EXCLUDE (rn) FROM ranked WHERE rn = 1
            """).to_arrow_table()
        finally:
            con.close()

    def test_ticker_wins_for_a_restated_series(self):
        """6669 on 2026-08-28: the market file kept the pre-ex-right basis."""
        out = self._dedup(
            [_raw_row(stock_id="6669", date="2026-08-28", close=2413.843901)],
            [_raw_row(stock_id="6669", date="2026-08-28", close=7200.0)],
            prefer="ticker",
        )
        assert out.num_rows == 1
        assert out.column("close").to_pylist() == [2413.843901]
        assert out.column("source").to_pylist() == ["ticker"]

    def test_market_still_fills_days_past_the_ticker_file(self):
        out = self._dedup(
            [_raw_row(date="2026-09-11")],
            [_raw_row(date="2026-09-11"), _raw_row(date="2026-09-14")],
            prefer="ticker",
        )
        by_date = dict(
            zip(
                (str(d) for d in out.column("date").to_pylist()),
                out.column("source").to_pylist(),
                strict=True,
            )
        )
        assert by_date == {"2026-09-11": "ticker", "2026-09-14": "market"}

    def test_configured_preference_per_dataset(self):
        settings = get_settings()
        assert settings.dataset("daily_prices").dedupe_prefer == "market"
        assert settings.dataset("adj_daily_prices").dedupe_prefer == "ticker"

    def test_unknown_preference_is_rejected(self):
        with pytest.raises(ValueError):
            _source_rank("newest")

    def test_market_wins_on_conflict(self):
        out = self._dedup(
            [_raw_row(close=100.0, volume=111)],
            [_raw_row(close=104.65, volume=222)],
        )
        assert out.num_rows == 1
        assert out.column("close").to_pylist() == [104.65]
        assert out.column("volume").to_pylist() == [222]
        assert out.column("source").to_pylist() == ["market"]

    def test_non_overlapping_rows_all_kept(self):
        out = self._dedup(
            [_raw_row(date="2026-08-20"), _raw_row(date="2026-08-21")],
            [_raw_row(date="2026-08-24")],
        )
        assert out.num_rows == 3

    def test_ticker_only_history_survives(self):
        """Backfilled history must not be dropped for lack of a market row."""
        out = self._dedup([_raw_row(date="2003-07-01")], [_raw_row(date="2026-08-21")])
        assert sorted(str(d) for d in out.column("date").to_pylist()) == [
            "2003-07-01",
            "2026-08-21",
        ]

    def test_null_dates_are_dropped(self):
        out = self._dedup([_raw_row(date=None)], [_raw_row()])
        assert out.num_rows == 1


class TestTradingCalendarHelpers:
    @pytest.fixture
    def cal(self):
        return [dt.date(2026, 8, d) for d in (17, 18, 19, 20, 21, 24, 25, 26)]

    def test_between_is_inclusive(self, cal):
        got = trading_days_between(cal, "2026-08-19", "2026-08-21")
        assert got == [dt.date(2026, 8, 19), dt.date(2026, 8, 20), dt.date(2026, 8, 21)]

    def test_missing_dates_finds_holes(self, cal):
        present = {dt.date(2026, 8, d) for d in (17, 18, 20, 21, 24, 25, 26)}
        assert missing_dates(cal, present) == [dt.date(2026, 8, 19)]

    def test_missing_dates_empty_when_complete(self, cal):
        assert missing_dates(cal, set(cal)) == []

    def test_last_closed_ignores_future_dates(self, cal):
        """The calendar runs to year end, so max() is not 'latest data'."""
        assert last_closed_trading_day(cal, dt.date(2026, 8, 21)) == dt.date(2026, 8, 21)
        assert last_closed_trading_day(cal, dt.date(2026, 8, 23)) == dt.date(2026, 8, 21)

    def test_shift_backwards(self, cal):
        # from 08-26: one day back is 08-25, two is 08-24 (08-22/23 are a weekend)
        assert shift_trading_days(cal, dt.date(2026, 8, 26), -2) == dt.date(2026, 8, 24)
        assert shift_trading_days(cal, dt.date(2026, 8, 26), -5) == dt.date(2026, 8, 19)

    def test_shift_clamps_at_start(self, cal):
        assert shift_trading_days(cal, dt.date(2026, 8, 17), -99) == cal[0]

    def test_to_date_accepts_both_forms(self):
        assert to_date("2026-08-21") == to_date(dt.date(2026, 8, 21))


class TestCalendarExpansionGolden:
    """The expanded calendar must reproduce the user's own prior expansion."""

    def test_expanded_is_superset_of_official(self, golden_calendar, golden_calendar_expanded):
        official = set(golden_calendar.column("date").to_pylist())
        expanded = set(golden_calendar_expanded.column("date").to_pylist())
        assert official <= expanded

    def test_expansion_adds_1204_pre_1999_days(self, golden_calendar, golden_calendar_expanded):
        official = set(golden_calendar.column("date").to_pylist())
        expanded = set(golden_calendar_expanded.column("date").to_pylist())
        extra = expanded - official
        assert len(extra) == 1204
        assert max(extra) < "1999-01-01", "all derived days must predate the official calendar"

    def test_derived_segment_includes_saturdays(self, golden_calendar, golden_calendar_expanded):
        """Taiwan traded on Saturdays until 1998 — weekday-only logic is wrong."""
        official = set(golden_calendar.column("date").to_pylist())
        extra = set(golden_calendar_expanded.column("date").to_pylist()) - official
        saturdays = [d for d in extra if dt.date.fromisoformat(d).weekday() == 5]
        assert len(saturdays) > 100, f"expected many Saturdays, found {len(saturdays)}"
