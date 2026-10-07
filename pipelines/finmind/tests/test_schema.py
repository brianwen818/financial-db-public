"""Schema contract tests.

Each of these pins a specific data-quality defect found in the real FinMind
responses. A regression here means bad data silently reaching the query layer.
"""

from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pytest

from finmind_pipeline.processing.schema import (
    PRICE_RENAME,
    PROCESSED_PRICE_SCHEMA,
    PROCESSED_STOCK_INFO_SCHEMA,
    RAW_PRICE_SCHEMA,
    RAW_STOCK_INFO_SCHEMA,
    clean_str,
    enforce,
    parse_date,
    records_to_raw_table,
)


class TestNoneSentinel:
    """TaiwanStockInfo sends the literal string "None" for 32 index rows."""

    @pytest.mark.parametrize("value", ["None", "none", "", "nan", "NaN", "null", None])
    def test_sentinels_become_null(self, value):
        assert parse_date(value) is None
        assert clean_str(value) is None

    def test_real_date_parses(self):
        assert parse_date("2026-08-21") == dt.date(2026, 8, 21)

    def test_unparseable_does_not_raise(self):
        assert parse_date("not-a-date") is None
        assert parse_date("2026-13-45") is None

    def test_golden_stock_info_has_exactly_32_none_dates(self, golden_stock_info):
        dates = golden_stock_info.column("date").to_pylist()
        assert sum(1 for d in dates if d == "None") == 32, (
            "the sample must still contain the 32 literal 'None' rows this "
            "pipeline is built to handle"
        )
        assert sum(1 for d in dates if parse_date(d) is None) == 32


class TestStockIdIsText:
    """stock_id is not numeric and leading zeros carry meaning."""

    def test_leading_zeros_preserved(self):
        table = records_to_raw_table([{"date": "2026-08-21", "stock_id": "0050"}], RAW_PRICE_SCHEMA)
        assert table.column("stock_id").to_pylist() == ["0050"]

    @pytest.mark.parametrize(
        "stock_id", ["0050", "00679B", "00400A", "TAIEX", "TradingConsumersGoods"]
    )
    def test_non_numeric_ids_round_trip(self, stock_id):
        table = records_to_raw_table([{"stock_id": stock_id}], RAW_STOCK_INFO_SCHEMA)
        assert table.column("stock_id").to_pylist() == [stock_id]

    def test_schema_declares_string(self):
        assert RAW_PRICE_SCHEMA.field("stock_id").type == pa.string()
        assert PROCESSED_PRICE_SCHEMA.field("stock_id").type == pa.string()


class TestColumnRenaming:
    """max/min collide with SQL aggregates and are renamed to high/low."""

    def test_rename_map(self):
        assert PRICE_RENAME["max"] == "high"
        assert PRICE_RENAME["min"] == "low"
        assert PRICE_RENAME["Trading_Volume"] == "volume"
        assert PRICE_RENAME["Trading_money"] == "turnover_value"
        assert PRICE_RENAME["Trading_turnover"] == "transactions"

    def test_processed_schema_has_no_reserved_names(self):
        names = set(PROCESSED_PRICE_SCHEMA.names)
        assert {"high", "low"} <= names
        assert not ({"max", "min"} & names)

    def test_raw_schema_keeps_upstream_names(self):
        assert {"max", "min"} <= set(RAW_PRICE_SCHEMA.names)


class TestNumericWidths:
    """Trading_money reaches 5.3e10 and overflows int32."""

    def test_turnover_exceeds_int32(self):
        big = 52_822_527_292
        assert big > 2**31 - 1
        table = records_to_raw_table([{"Trading_money": big}], RAW_PRICE_SCHEMA)
        assert table.column("Trading_money").to_pylist() == [big]

    def test_count_columns_are_int64(self):
        for col in ("volume", "turnover_value", "transactions"):
            assert PROCESSED_PRICE_SCHEMA.field(col).type == pa.int64()

    def test_float_valued_ints_are_coerced(self):
        table = records_to_raw_table([{"Trading_Volume": 41340817.0}], RAW_PRICE_SCHEMA)
        assert table.column("Trading_Volume").to_pylist() == [41340817]


class TestRobustness:
    def test_missing_column_becomes_null_not_error(self):
        table = records_to_raw_table([{"date": "2026-08-21"}], RAW_PRICE_SCHEMA)
        assert table.num_rows == 1
        assert table.column("close").to_pylist() == [None]

    def test_empty_records_yields_empty_table_with_schema(self):
        table = records_to_raw_table([], RAW_PRICE_SCHEMA)
        assert table.num_rows == 0
        assert table.schema == RAW_PRICE_SCHEMA

    def test_enforce_rejects_missing_required_column(self):
        with pytest.raises(ValueError, match="missing required columns"):
            enforce(pa.table({"date": [dt.date(2026, 8, 21)]}), PROCESSED_STOCK_INFO_SCHEMA)
