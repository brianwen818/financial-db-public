from __future__ import annotations

import datetime as dt

from tej_pipeline.ingestion.trading_dates import missing_dates_for_workbooks


def test_missing_dates_are_seeded_only_for_current_workbooks(monkeypatch, tmp_path):
    current = tmp_path / "2330.xlsx"
    delisted = tmp_path / "2448.xlsx"
    current.touch()
    delisted.touch()

    latest = {
        "2330": dt.date(2026, 9, 2),
        "2448": dt.date(2021, 1, 5),
    }
    monkeypatch.setattr(
        "tej_pipeline.ingestion.trading_dates.latest_workbook_date",
        lambda path: latest[path.stem],
    )
    calendar = (
        dt.date(2026, 9, 2),
        dt.date(2026, 9, 3),
        dt.date(2026, 9, 4),
    )

    plans, market_latest = missing_dates_for_workbooks(
        [current, delisted],
        [current, delisted],
        calendar,
    )

    assert market_latest == dt.date(2026, 9, 2)
    assert plans["2330"] == (dt.date(2026, 9, 3), dt.date(2026, 9, 4))
    assert plans["2448"] == ()


def test_partial_refresh_does_not_make_yesterdays_workbooks_inactive(monkeypatch, tmp_path):
    yesterday = tmp_path / "0050.xlsx"
    today = tmp_path / "2330.xlsx"
    yesterday.touch()
    today.touch()
    latest = {
        "0050": dt.date(2026, 9, 2),
        "2330": dt.date(2026, 9, 3),
    }
    monkeypatch.setattr(
        "tej_pipeline.ingestion.trading_dates.latest_workbook_date",
        lambda path: latest[path.stem],
    )
    calendar = tuple(dt.date(2026, 8, 1) + dt.timedelta(days=i) for i in range(34))

    plans, _ = missing_dates_for_workbooks(
        [yesterday, today],
        [yesterday, today],
        calendar,
    )

    assert plans["0050"] == (dt.date(2026, 9, 3),)
    assert plans["2330"] == ()
