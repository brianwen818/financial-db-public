"""Atomicity and filesystem-safety tests for the Parquet layer."""

from __future__ import annotations

import datetime as dt
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finmind_pipeline.utils.parquet_io import (
    StagedDirectory,
    atomic_write_table,
    day_partition_path,
    row_count,
    safe_filename,
)


def _table(n: int = 3) -> pa.Table:
    return pa.table({"stock_id": ["0050"] * n, "close": [float(i) for i in range(n)]})


class TestSafeFilename:
    @pytest.mark.parametrize(
        "stock_id", ["0050", "2330", "00679B", "00400A", "TradingConsumersGoods", "TAIEX"]
    )
    def test_normal_ids_unchanged(self, stock_id):
        assert safe_filename(stock_id) == stock_id

    @pytest.mark.parametrize("reserved", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT9", "nul"])
    def test_windows_reserved_names_are_escaped(self, reserved):
        """Windows refuses to create these regardless of extension."""
        out = safe_filename(reserved)
        assert out != reserved
        assert out.startswith(f"_{reserved}_")

    @pytest.mark.parametrize("bad", ["a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b'])
    def test_illegal_characters_replaced(self, bad):
        assert "/" not in safe_filename(bad)
        assert "\\" not in safe_filename(bad)

    def test_empty_result_raises(self):
        with pytest.raises(ValueError):
            safe_filename(".")

    def test_distinct_ids_stay_distinct(self):
        """Two securities must never share a file, or their histories merge."""
        ids = ["0050", "CON", "_CON", "TAIEX", "a/b", "a\b", "a:b", "0050 ", "0050."]
        assert len({safe_filename(i) for i in ids}) == len(ids)


class TestDayPartitionPath:
    def test_layout(self, tmp_path):
        p = day_partition_path(tmp_path, "2024-03-15")
        assert p.parent.name == "month=03"
        assert p.parent.parent.name == "year=2024"
        assert p.name == "2024-03-15.parquet"

    def test_accepts_date_object(self, tmp_path):
        assert day_partition_path(tmp_path, dt.date(1994, 10, 1)).name == "1994-10-01.parquet"

    def test_single_digit_month_zero_padded(self, tmp_path):
        assert day_partition_path(tmp_path, "2024-01-05").parent.name == "month=01"


class TestAtomicWrite:
    def test_writes_and_reports_rows(self, tmp_path):
        target = tmp_path / "a" / "b" / "x.parquet"
        assert atomic_write_table(_table(5), target) == 5
        assert row_count(target) == 5

    def test_no_tmp_file_left_behind(self, tmp_path):
        target = tmp_path / "x.parquet"
        atomic_write_table(_table(), target)
        assert list(tmp_path.glob("*.tmp")) == []

    def test_interrupted_write_leaves_target_intact(self, tmp_path):
        """A killed process must never leave a half-written Parquet in place."""
        target = tmp_path / "x.parquet"
        atomic_write_table(_table(5), target)

        with mock.patch.object(pq, "write_table", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                atomic_write_table(_table(99), target)

        assert row_count(target) == 5, "original content must survive"
        assert list(tmp_path.glob("*.tmp")) == [], "no partial file left"

    def test_overwrite_replaces_content(self, tmp_path):
        target = tmp_path / "x.parquet"
        atomic_write_table(_table(2), target)
        atomic_write_table(_table(7), target)
        assert row_count(target) == 7


class TestStagedDirectory:
    def test_swap_replaces_contents(self, tmp_path):
        live = tmp_path / "ds"
        live.mkdir()
        atomic_write_table(_table(), live / "old.parquet")

        with StagedDirectory(live) as stage:
            atomic_write_table(_table(), stage / "new.parquet")

        assert [p.name for p in live.iterdir()] == ["new.parquet"]

    def test_failure_leaves_live_untouched(self, tmp_path):
        """A failed rebuild must not damage a queryable dataset."""
        live = tmp_path / "ds"
        live.mkdir()
        atomic_write_table(_table(4), live / "old.parquet")

        with pytest.raises(RuntimeError):
            with StagedDirectory(live) as stage:
                atomic_write_table(_table(), stage / "partial.parquet")
                raise RuntimeError("rebuild blew up")

        assert [p.name for p in live.iterdir()] == ["old.parquet"]
        assert row_count(live / "old.parquet") == 4

    def test_works_when_live_does_not_exist_yet(self, tmp_path):
        live = tmp_path / "brand-new"
        with StagedDirectory(live) as stage:
            atomic_write_table(_table(), stage / "a.parquet")
        assert (live / "a.parquet").exists()

    def test_no_staging_or_backup_dirs_remain(self, tmp_path):
        live = tmp_path / "ds"
        with StagedDirectory(live) as stage:
            atomic_write_table(_table(), stage / "a.parquet")
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "ds"]
        assert leftovers == []
