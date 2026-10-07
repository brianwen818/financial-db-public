"""Tests for confirmed-empty trading days.

The published exchange calendar is not perfectly accurate: 2026-07-10 is listed
as a trading day, but the whole-market endpoint returns zero securities for it.
Recording that keeps the gap report meaningful. Recording it *wrongly* would
blind the pipeline to real gaps, so the guard rule is what matters most here.
"""

from __future__ import annotations

import datetime as dt

import pytest

from finmind_pipeline.processing import empty_dates

D = dt.date


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Point the module at a scratch file so tests never touch real data."""
    target = tmp_path / "known_empty_dates.parquet"
    monkeypatch.setattr(empty_dates, "path", lambda: target)
    return target


class TestSafetyRule:
    """An empty response only means "closed" for a day already in the past."""

    def test_does_not_record_dates_at_or_after_newest_held(self):
        newest = D(2026, 8, 25)
        empty_dates.record([D(2026, 8, 25), D(2026, 8, 26)], newest_held=newest)
        assert empty_dates.load() == set(), (
            "an unpublished day must never be recorded as closed, or real gaps "
            "become invisible"
        )

    def test_records_a_genuine_past_closure(self):
        empty_dates.record([D(2026, 7, 10)], newest_held=D(2026, 8, 25))
        assert empty_dates.load() == {D(2026, 7, 10)}

    def test_mixed_batch_keeps_only_the_past_ones(self):
        empty_dates.record(
            [D(2026, 7, 10), D(2026, 8, 25), D(2026, 8, 26)], newest_held=D(2026, 8, 25)
        )
        assert empty_dates.load() == {D(2026, 7, 10)}

    def test_no_newest_held_records_everything(self):
        """With nothing held there is no 'unpublished' notion to protect."""
        empty_dates.record([D(2026, 7, 10)], newest_held=None)
        assert empty_dates.load() == {D(2026, 7, 10)}


class TestAccumulation:
    def test_is_additive_across_runs(self):
        empty_dates.record([D(2026, 7, 10)], newest_held=D(2026, 8, 25))
        empty_dates.record([D(2025, 5, 1)], newest_held=D(2026, 8, 25))
        assert empty_dates.load() == {D(2026, 7, 10), D(2025, 5, 1)}

    def test_recording_the_same_date_twice_is_idempotent(self):
        empty_dates.record([D(2026, 7, 10)], newest_held=D(2026, 8, 25))
        empty_dates.record([D(2026, 7, 10)], newest_held=D(2026, 8, 25))
        assert empty_dates.load() == {D(2026, 7, 10)}

    def test_empty_input_is_a_no_op(self, isolated):
        assert empty_dates.record([], newest_held=D(2026, 8, 25)) == set()
        assert not isolated.exists()


class TestFileHandling:
    def test_load_on_missing_file_returns_empty_set(self):
        assert empty_dates.load() == set()

    def test_ensure_exists_creates_a_readable_file(self, isolated):
        empty_dates.ensure_exists()
        assert isolated.exists()
        assert empty_dates.load() == set()

    def test_ensure_exists_does_not_clobber_existing_data(self):
        empty_dates.record([D(2026, 7, 10)], newest_held=D(2026, 8, 25))
        empty_dates.ensure_exists()
        assert empty_dates.load() == {D(2026, 7, 10)}
