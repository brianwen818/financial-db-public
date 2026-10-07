"""Security-code shape classification and the discovery sweep's sampling.

The shape rules are the only thing standing between a 12M-row warehouse and a
97M-row one, and the codes they have to separate are genuinely ambiguous:
00679B is a bond ETF, 91009T is a TDR, 0347 is a pre-2007 warrant, and all
three are digits-then-maybe-a-letter. Every case here is a real code observed
in the 271-day sweep.
"""

from __future__ import annotations

import datetime as dt

import pytest

from finmind_pipeline.ingestion.universe_discovery import (
    Observation,
    SweepResult,
    monthly_sample_days,
    to_table,
)
from finmind_pipeline.processing.universe import (
    IN_SCOPE_SHAPES,
    classify_stock_id,
    in_scope,
)


class TestClassification:
    @pytest.mark.parametrize(
        ("stock_id", "shape"),
        [
            # ordinary shares, listed and delisted
            ("2330", "common"),
            ("9915", "common"),
            ("1101", "common"),
            # funds: two, three and four digits after the 00, with or without
            # a class letter. 00679B must not read as a TDR.
            ("0050", "etf"),
            ("00631", "etf"),
            ("00679B", "etf"),
            ("00632R", "etf"),
            ("006202", "etf"),
            # TDRs and beneficiary certificates
            ("91009T", "tdr"),
            ("910322", "warrant"),
            # warrants: modern six-digit, five-digit, and the pre-2007 forms
            ("708785", "warrant"),
            ("03417", "warrant"),
            ("23442", "warrant"),
            ("0347", "warrant"),
            ("0617P", "warrant"),
            # preferred shares and convertible-bond series share one shape
            ("1101A", "preferred"),
            ("2801A", "preferred"),
            ("2418W", "preferred"),
            # index pseudo-tickers
            ("TAIEX", "index"),
            ("Textiles", "index"),
            ("TradingConsumersGoods", "index"),
        ],
    )
    def test_shape(self, stock_id, shape):
        assert classify_stock_id(stock_id) == shape

    def test_empty_is_other_not_an_error(self):
        assert classify_stock_id("") == "other"

    def test_only_common_and_etf_are_in_scope(self):
        assert IN_SCOPE_SHAPES == {"common", "etf"}
        assert in_scope("2330") and in_scope("0050")
        assert not in_scope("708785")
        assert not in_scope("91009T")
        assert not in_scope("1101A")

    def test_leading_zero_shares_are_never_common(self):
        """0347 is a warrant, not share number 347. Getting this wrong would
        pull 108,000 warrants into the universe."""
        assert classify_stock_id("0347") != "common"


class TestMonthlySampling:
    @staticmethod
    def _calendar(start: dt.date, days: int) -> list[dt.date]:
        return [start + dt.timedelta(days=i) for i in range(days)]

    def test_one_day_per_month(self):
        calendar = self._calendar(dt.date(2010, 1, 1), 365)
        days = monthly_sample_days(calendar, dt.date(2010, 1, 1), dt.date(2010, 12, 31))
        assert len(days) == 12
        assert [d.month for d in days] == list(range(1, 13))

    def test_picks_the_middle_of_the_month(self):
        """Not the first or last trading day: month ends carry settlement
        effects and the Lunar New Year gap distorts month starts."""
        calendar = self._calendar(dt.date(2010, 3, 1), 31)
        (day,) = monthly_sample_days(calendar, dt.date(2010, 3, 1), dt.date(2010, 3, 31))
        assert day == dt.date(2010, 3, 16)

    def test_respects_bounds(self):
        calendar = self._calendar(dt.date(2010, 1, 1), 365)
        days = monthly_sample_days(calendar, dt.date(2010, 6, 1), dt.date(2010, 8, 31))
        assert [d.month for d in days] == [6, 7, 8]

    def test_sparse_month_still_yields_a_day(self):
        calendar = [dt.date(2010, 2, 8)]
        assert monthly_sample_days(calendar, dt.date(2010, 1, 1)) == [dt.date(2010, 2, 8)]


class TestSweepTable:
    def test_observation_tracks_span_and_hits(self):
        obs = Observation("2330")
        for d in (dt.date(2010, 5, 1), dt.date(2008, 1, 1), dt.date(2012, 9, 1)):
            obs.record(d)
        assert obs.hits == 3
        assert obs.first_seen == dt.date(2008, 1, 1)
        assert obs.last_seen == dt.date(2012, 9, 1)

    def test_table_carries_shape_and_sample_size(self):
        result = SweepResult(days_sampled=[dt.date(2010, 1, 1), dt.date(2010, 2, 1)])
        for stock_id in ("2330", "708785"):
            obs = Observation(stock_id)
            obs.record(dt.date(2010, 1, 1))
            result.observations[stock_id] = obs

        table = to_table(result)
        rows = {r["stock_id"]: r for r in table.to_pylist()}
        assert rows["2330"]["shape"] == "common"
        assert rows["708785"]["shape"] == "warrant"
        # sampled_days is what makes sample_hits comparable across sweeps
        assert all(r["sampled_days"] == 2 for r in rows.values())

    def test_out_of_scope_codes_are_recorded_not_dropped(self):
        """Warrants stay in the snapshot so that widening scope later is a
        config change rather than another 271 API calls."""
        result = SweepResult(days_sampled=[dt.date(2010, 1, 1)])
        obs = Observation("708785")
        obs.record(dt.date(2010, 1, 1))
        result.observations["708785"] = obs
        assert to_table(result).num_rows == 1
