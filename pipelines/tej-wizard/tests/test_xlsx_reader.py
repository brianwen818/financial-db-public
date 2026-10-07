from __future__ import annotations

import datetime as dt
import shutil
import zipfile

import pyarrow.compute as pc
import pytest

from tej_pipeline.processing import schema as S
from tej_pipeline.processing.xlsx_reader import WorkbookFormatError, read_workbook


def test_reads_a_real_workbook(sample_workbook):
    table = read_workbook(sample_workbook)
    assert table.num_rows > 0
    assert table.schema.equals(S.PROCESSED_SCHEMA)
    # One workbook is one security; the filename is that security's id.
    ids = pc.unique(table.column("stock_id").combine_chunks()).to_pylist()
    assert ids == [sample_workbook.stem]


def test_dates_are_real_dates_not_serials(sample_workbook):
    table = read_workbook(sample_workbook)
    dates = table.column("date").to_pylist()
    assert all(isinstance(d, dt.date) for d in dates)
    # The whole dataset starts in 2000; a serial read as a date would land in
    # 1970 or throw, and a date read as a serial would show up as a huge int.
    assert min(dates) >= dt.date(2000, 1, 1)
    assert max(dates) <= dt.date.today() + dt.timedelta(days=7)


def test_no_duplicate_dates_within_a_workbook(sample_workbook):
    table = read_workbook(sample_workbook)
    dates = table.column("date").to_pylist()
    assert len(dates) == len(set(dates))


def test_absent_cells_become_null_not_shifted_columns(sample_workbook):
    """The flag columns are mostly empty and are the last columns in the sheet.

    If a reader mapped cells positionally instead of by their `r` reference,
    every row with a missing flag would shift `market` out of its column, so a
    clean `market` domain is the evidence that keying by reference works.
    """
    table = read_workbook(sample_workbook)
    markets = set(pc.unique(table.column("market").combine_chunks()).to_pylist())
    assert markets <= {"TSE", "OTC", "REG", "TIB", "PSB"}
    flags = set(pc.unique(table.column("limit_flag").combine_chunks()).to_pylist())
    assert flags <= {"+", "-", None}


def test_adjusted_and_raw_prices_are_on_different_scales(raw_dir):
    """2330's oldest bar: adjusted close 27.99 against a raw reference of 133.50.

    This is the dataset's sharpest trap, so it is pinned as a test rather than
    only documented.
    """
    path = raw_dir / "2330.xlsx"
    if not path.exists():
        pytest.skip("2330.xlsx not present")
    table = read_workbook(path)
    oldest = table.sort_by([("date", "ascending")]).slice(0, 1)
    assert oldest.column("date")[0].as_py() == dt.date(2000, 9, 4)
    assert oldest.column("next_ref_price")[0].as_py() == 133.5
    assert oldest.column("close")[0].as_py() < 30  # back-adjusted, far below 133.5


def test_rejects_a_workbook_with_the_wrong_header(tmp_path, sample_workbook):
    """A different TEJ query must not be parsed as if it were this one."""
    broken = tmp_path / "broken.xlsx"
    shutil.copy2(sample_workbook, broken)

    with zipfile.ZipFile(broken) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    # Rename the first header, which lives in sharedStrings.
    parts["xl/sharedStrings.xml"] = parts["xl/sharedStrings.xml"].replace(
        "<t>證券代碼</t>".encode(), "<t>公司代碼</t>".encode()
    )
    with zipfile.ZipFile(broken, "w") as z:
        for name, data in parts.items():
            z.writestr(name, data)

    with pytest.raises(WorkbookFormatError, match="header layout"):
        read_workbook(broken)


def test_ingested_at_is_stamped(sample_workbook):
    ts = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    table = read_workbook(sample_workbook, ingested_at=ts)
    assert table.column("ingested_at")[0].as_py() == ts
    assert table.column("source_file")[0].as_py() == sample_workbook.name
