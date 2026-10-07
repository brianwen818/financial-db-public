from __future__ import annotations

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tej_pipeline.utils import parquet_io as P

ROOT = Path("/root")


def test_day_partition_path():
    p = P.partition_path(ROOT, dt.date(2024, 3, 7), "day")
    assert p == ROOT / "year=2024" / "month=03" / "2024-03-07.parquet"


def test_month_partition_path():
    p = P.partition_path(ROOT, dt.date(2024, 3, 7), "month")
    # Same Hive keys as the day grain, so the same `year` pruning rule applies.
    assert p == ROOT / "year=2024" / "month=03" / "2024-03.parquet"


def test_partition_path_accepts_iso_string():
    assert P.partition_path(ROOT, "2024-03-07", "day").name == "2024-03-07.parquet"


def test_partition_path_rejects_unknown_grain():
    with pytest.raises(ValueError, match="partition grain"):
        P.partition_path(ROOT, dt.date(2024, 3, 7), "week")


def test_atomic_write_leaves_no_temp_file(tmp_path):
    table = pa.table({"a": [1, 2, 3]})
    target = tmp_path / "sub" / "x.parquet"
    assert P.atomic_write_table(table, target) == 3
    assert target.exists()
    assert list(tmp_path.rglob("*.tmp")) == []
    assert pq.read_table(target).num_rows == 3


def test_atomic_write_cleans_up_on_failure(tmp_path):
    target = tmp_path / "x.parquet"
    with pytest.raises(AttributeError):
        P.atomic_write_table("not a table", target)  # type: ignore[arg-type]
    # The half-written temp file must not survive, or the next scan trips on it.
    assert list(tmp_path.rglob("*.tmp")) == []


def test_staged_directory_swaps_in_on_success(tmp_path):
    live = tmp_path / "data"
    live.mkdir()
    (live / "old.parquet").write_bytes(b"old")

    with P.StagedDirectory(live) as staging:
        P.atomic_write_table(pa.table({"a": [1]}), staging / "new.parquet")

    assert (live / "new.parquet").exists()
    assert not (live / "old.parquet").exists()
    assert not live.with_name("data__staging").exists()
    assert not live.with_name("data__old").exists()


def test_staged_directory_leaves_live_untouched_on_failure(tmp_path):
    live = tmp_path / "data"
    live.mkdir()
    (live / "old.parquet").write_bytes(b"old")

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with P.StagedDirectory(live) as staging:
            P.atomic_write_table(pa.table({"a": [1]}), staging / "new.parquet")
            raise Boom()

    # The whole point of staging: a failed rebuild must not destroy the data
    # that is already there.
    assert (live / "old.parquet").exists()
    assert not (live / "new.parquet").exists()
    assert not live.with_name("data__staging").exists()
