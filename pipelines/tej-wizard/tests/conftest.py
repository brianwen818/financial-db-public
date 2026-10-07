from __future__ import annotations

import datetime as dt
from pathlib import Path

import zipfile

import pyarrow as pa
import pytest

from tej_pipeline.processing import index_schema as IS
from tej_pipeline.processing import schema as S
from tej_pipeline.settings import get_settings


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def raw_dir(settings) -> Path:
    return settings.raw_dir_for("adj_daily_prices")


@pytest.fixture(scope="session")
def sample_workbook(raw_dir) -> Path:
    """The smallest real workbook, so tests that parse one stay fast."""
    from tej_pipeline.processing.prices import raw_workbooks

    books = raw_workbooks(raw_dir)
    if not books:
        pytest.skip(f"no workbooks in {raw_dir}")
    return min(books, key=lambda p: p.stat().st_size)


def make_table(rows: list[tuple[str, dt.date]]) -> pa.Table:
    """A minimal table on the processed schema, for partitioning tests."""
    n = len(rows)
    cols: dict[str, list] = {name: [None] * n for name in S.PROCESSED_COLUMNS}
    cols["stock_id"] = [r[0] for r in rows]
    cols["stock_name"] = ["x"] * n
    cols["date"] = [r[1] for r in rows]
    cols["source_file"] = ["x.xlsx"] * n
    cols["ingested_at"] = [dt.datetime(2026, 1, 1, tzinfo=dt.UTC)] * n
    return pa.table(cols, schema=S.PROCESSED_SCHEMA)


@pytest.fixture(scope="session")
def index_raw_dir(settings) -> Path:
    return settings.raw_dir_for("index_constituents")


@pytest.fixture(scope="session")
def sample_index_workbook(index_raw_dir) -> tuple[str, Path]:
    """The smallest real index workbook, as ``(index_id, path)``."""
    from tej_pipeline.processing.index_constituents import index_workbooks

    books = index_workbooks(index_raw_dir)
    if not books:
        pytest.skip(f"no index workbooks in {index_raw_dir}")
    return min(books, key=lambda pair: pair[1].stat().st_size)


def make_index_table(rows: list[tuple[str, dt.date, str]]) -> pa.Table:
    """A minimal table on the index processed schema, for key tests."""
    n = len(rows)
    cols: dict[str, list] = {name: [None] * n for name in IS.PROCESSED_COLUMNS}
    cols["index_id"] = [r[0] for r in rows]
    cols["index_name"] = ["x"] * n
    cols["date"] = [r[1] for r in rows]
    cols["stock_id"] = [r[2] for r in rows]
    cols["stock_name"] = ["y"] * n
    cols["source_file"] = [f"{r[0]}/{r[1].year}.xlsx" for r in rows]
    cols["ingested_at"] = [dt.datetime(2026, 1, 1, tzinfo=dt.UTC)] * n
    return pa.table(cols, schema=IS.PROCESSED_SCHEMA)


_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def write_index_sheet(path, rows: list[list[str | None]]) -> None:
    """Build the smallest zip the reader will accept, with literal cell text.

    ``t="str"`` puts the value inline instead of in sharedStrings, which the
    reader handles and which keeps the fixture to one XML part.
    """
    body = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{chr(65 + i)}{r}" t="str"><v>{v}</v></c>'
            for i, v in enumerate(row)
            if v is not None
        )
        body.append(f'<row r="{r}">{cells}</row>')
    xml = (
        f'<?xml version="1.0"?><worksheet xmlns="{_NS}"><sheetData>'
        + "".join(body)
        + "</sheetData></worksheet>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", xml.encode("utf-8"))
