from __future__ import annotations

import datetime as dt

import pytest
from conftest import make_table

from tej_pipeline.processing.prices import (
    RawLayerBusyError,
    assert_raw_readable,
    assert_unique_key,
    raw_workbooks,
)
from tej_pipeline.utils.parquet_io import partition_runs


def _runs(rows, grain):
    table = make_table(rows).sort_by([("date", "ascending")])
    return [(d, s, e) for d, s, e in partition_runs(table, grain)]


def test_day_runs_split_on_every_date():
    rows = [
        ("A", dt.date(2024, 1, 2)),
        ("B", dt.date(2024, 1, 2)),
        ("A", dt.date(2024, 1, 3)),
    ]
    runs = _runs(rows, "day")
    assert [(r[1], r[2]) for r in runs] == [(0, 2), (2, 3)]


def test_month_runs_group_dates_within_a_month():
    rows = [
        ("A", dt.date(2024, 1, 2)),
        ("A", dt.date(2024, 1, 31)),
        ("A", dt.date(2024, 2, 1)),
    ]
    runs = _runs(rows, "month")
    assert [(r[1], r[2]) for r in runs] == [(0, 2), (2, 3)]


def test_runs_cover_every_row_exactly_once():
    rows = [("A", dt.date(2024, 1, 1) + dt.timedelta(days=i)) for i in range(70)]
    for grain in ("day", "month"):
        runs = _runs(rows, grain)
        assert runs[0][1] == 0
        assert runs[-1][2] == len(rows)
        # No gaps and no overlaps between consecutive slices.
        assert all(a[2] == b[1] for a, b in zip(runs, runs[1:], strict=False))


def test_partition_runs_on_empty_table():
    assert _runs([], "day") == []


def test_unique_key_accepts_distinct_pairs():
    assert_unique_key(make_table([("A", dt.date(2024, 1, 1)), ("A", dt.date(2024, 1, 2))]))
    assert_unique_key(make_table([("A", dt.date(2024, 1, 1)), ("B", dt.date(2024, 1, 1))]))


def test_unique_key_rejects_a_duplicated_security_date():
    """Two workbooks covering one security would double-count it everywhere."""
    dup = make_table([("A", dt.date(2024, 1, 1)), ("A", dt.date(2024, 1, 1))])
    with pytest.raises(ValueError, match="not unique"):
        assert_unique_key(dup)


def test_raw_layer_refuses_to_read_while_excel_holds_a_workbook(tmp_path):
    (tmp_path / "2330.xlsx").write_bytes(b"x")
    assert_raw_readable(tmp_path)  # no lock file yet

    (tmp_path / "~$2330.xlsx").write_bytes(b"lock")
    with pytest.raises(RawLayerBusyError, match="2330"):
        assert_raw_readable(tmp_path)


def test_lock_files_are_not_mistaken_for_data(tmp_path):
    (tmp_path / "2330.xlsx").write_bytes(b"x")
    (tmp_path / "~$2330.xlsx").write_bytes(b"lock")
    assert [p.name for p in raw_workbooks(tmp_path)] == ["2330.xlsx"]
