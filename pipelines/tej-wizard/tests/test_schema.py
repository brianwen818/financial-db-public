from __future__ import annotations

import datetime as dt

import pytest

from tej_pipeline.processing import schema as S


def test_column_contract_is_35_wide():
    assert len(S.COLUMNS) == 35
    assert len(S.RAW_HEADERS) == 35
    # Two identity columns carry no TEJ field code; the rest map 1:1 onto the
    # field list in the workbook's own query definition.
    assert len(S.TEJ_FIELD_CODES) == 33


def test_processed_names_are_sql_safe_and_unique():
    names = [c[3] for c in S.COLUMNS if c[3] != "_security"]
    assert len(names) == len(set(names))
    for name in names:
        assert name.islower()
        assert name.replace("_", "").isalnum(), name
    # `max`/`min` collide with SQL aggregates; TEJ does not use them, but the
    # equivalent trap here is a bare `open`/`close`, which DuckDB accepts.
    assert "high" in names and "low" in names


def test_processed_schema_matches_columns():
    # 3 identity/date + 33 data + 2 provenance
    assert len(S.PROCESSED_COLUMNS) == 38
    assert S.PROCESSED_COLUMNS[:3] == ("stock_id", "stock_name", "date")
    assert S.PROCESSED_COLUMNS[-2:] == ("source_file", "ingested_at")


@pytest.mark.parametrize(
    "serial,expected",
    [
        (36773, dt.date(2000, 9, 4)),  # oldest bar in the dataset
        (46267, dt.date(2026, 9, 2)),  # newest at build time
        (41684, dt.date(2014, 2, 14)),  # 8078 華寶's last bar
    ],
)
def test_excel_serial_to_date(serial, expected):
    assert S.excel_serial_to_date(serial) == expected


def test_excel_serial_rejects_the_1900_leap_bug_range():
    # Serials <= 60 are ambiguous because Excel believes 1900 was a leap year.
    # Nothing in this dataset reaches there, so hitting it means bad input.
    with pytest.raises(ValueError, match="leap-bug"):
        S.excel_serial_to_date(59)


def test_excel_serial_accepts_strings_and_floats():
    assert S.excel_serial_to_date("36773") == dt.date(2000, 9, 4)
    assert S.excel_serial_to_date(36773.0) == dt.date(2000, 9, 4)


@pytest.mark.parametrize(
    "cell,stock_id,name",
    [
        ("2330 台積電", "2330", "台積電"),
        ("0050 元大台灣50", "0050", "元大台灣50"),  # leading zero must survive
        ("3697 F-晨星", "3697", "F-晨星"),
        ("  1101 台泥  ", "1101", "台泥"),
        ("9999", "9999", None),  # id with no name -> NULL, not ""
    ],
)
def test_split_security(cell, stock_id, name):
    assert S.split_security(cell) == (stock_id, name)


def test_split_security_rejects_empty():
    with pytest.raises(ValueError):
        S.split_security("   ")


def test_adjusted_and_raw_price_columns_are_disjoint():
    # The whole point of tracking both sets is that they are on different
    # scales; an overlap would mean a column is documented as being on both.
    assert not (S.ADJUSTED_PRICE_COLUMNS & S.UNADJUSTED_PRICE_COLUMNS)
