"""Canonical schemas and column mappings. The single source of truth.

Two schema families exist for every dataset:

**raw** mirrors the API response exactly — same column names, same types, dates
left as the ``YYYY-MM-DD`` strings FinMind sends. Nothing is interpreted, so a
raw file can always be replayed.

**processed** is the query contract: real ``DATE`` columns, SQL-friendly names,
deduplicated. DuckDB views read only this layer.

Fixes encoded here, all confirmed against the sample data:

* ``max`` / ``min`` are renamed to ``high`` / ``low`` — the originals collide
  with SQL aggregate names and need quoting everywhere otherwise.
* ``Trading_money`` reaches 5.3e10, far beyond int32, so every count column is
  int64.
* ``stock_id`` stays VARCHAR: leading zeros are significant (``0050``) and the
  values are not all numeric (``00679B``, ``TAIEX``, ``TradingConsumersGoods``).
* ``TaiwanStockInfo.date`` contains the literal string ``"None"`` on 32 rows
  (the index pseudo-tickers). It must become a real NULL, never a failed cast.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pyarrow as pa

NONE_SENTINELS = {"None", "none", "NaN", "nan", "", "null", "NULL"}

# ---------------------------------------------------------------------------
# prices  (TaiwanStockPrice / TaiwanStockPriceAdj share one shape)
# ---------------------------------------------------------------------------

RAW_PRICE_SCHEMA = pa.schema(
    [
        ("date", pa.string()),
        ("stock_id", pa.string()),
        ("Trading_Volume", pa.int64()),
        ("Trading_money", pa.int64()),
        ("open", pa.float64()),
        ("max", pa.float64()),
        ("min", pa.float64()),
        ("close", pa.float64()),
        ("spread", pa.float64()),
        ("Trading_turnover", pa.int64()),
    ]
)

PRICE_RENAME: dict[str, str] = {
    "date": "date",
    "stock_id": "stock_id",
    "open": "open",
    "max": "high",
    "min": "low",
    "close": "close",
    "spread": "spread",
    "Trading_Volume": "volume",
    "Trading_money": "turnover_value",
    "Trading_turnover": "transactions",
}

PROCESSED_PRICE_SCHEMA = pa.schema(
    [
        ("date", pa.date32()),
        ("stock_id", pa.string()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("spread", pa.float64()),
        ("volume", pa.int64()),
        ("turnover_value", pa.int64()),
        ("transactions", pa.int64()),
        ("source", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
    ]
)

# Which row wins when the same (stock_id, date) arrives from both fetch modes.
SOURCE_PRIORITY = {"market": 2, "ticker": 1}

# ---------------------------------------------------------------------------
# TaiwanStockInfo
# ---------------------------------------------------------------------------

RAW_STOCK_INFO_SCHEMA = pa.schema(
    [
        ("industry_category", pa.string()),
        ("stock_id", pa.string()),
        ("stock_name", pa.string()),
        ("type", pa.string()),
        ("date", pa.string()),
    ]
)

PROCESSED_STOCK_INFO_SCHEMA = pa.schema(
    [
        ("snapshot_date", pa.date32()),
        ("stock_id", pa.string()),
        ("stock_name", pa.string()),
        ("industry_category", pa.string()),
        ("industry_category_norm", pa.string()),
        ("type", pa.string()),
        ("is_index", pa.bool_()),
    ]
)

# ---------------------------------------------------------------------------
# TaiwanStockIndustryChain
# ---------------------------------------------------------------------------

RAW_INDUSTRY_SCHEMA = pa.schema(
    [
        ("stock_id", pa.string()),
        ("industry", pa.string()),
        ("sub_industry", pa.string()),
        ("date", pa.string()),
    ]
)

PROCESSED_INDUSTRY_SCHEMA = pa.schema(
    [
        ("stock_id", pa.string()),
        ("industry", pa.string()),
        ("sub_industry", pa.string()),
        ("snapshot_date", pa.date32()),
    ]
)

# ---------------------------------------------------------------------------
# TaiwanStockDelisting
# ---------------------------------------------------------------------------

RAW_DELISTING_SCHEMA = pa.schema(
    [
        ("date", pa.string()),
        ("stock_id", pa.string()),
        ("stock_name", pa.string()),
    ]
)

PROCESSED_DELISTING_SCHEMA = pa.schema(
    [
        ("stock_id", pa.string()),
        ("stock_name", pa.string()),
        ("delisted_date", pa.date32()),
    ]
)

# ---------------------------------------------------------------------------
# discovered universe — codes found by sampling whole-market days
# ---------------------------------------------------------------------------
#
# Not a FinMind dataset: this is what a whole-market sweep observed. It exists
# because TaiwanStockInfo covers only currently listed securities, so a code
# that delisted before the first snapshot has no other way into the universe.
# `sampled_days` records how many days the sweep looked at, which is what makes
# `sample_hits` interpretable across sweeps of different densities.

RAW_DISCOVERED_UNIVERSE_SCHEMA = pa.schema(
    [
        ("stock_id", pa.string()),
        ("shape", pa.string()),
        ("first_seen", pa.date32()),
        ("last_seen", pa.date32()),
        ("sample_hits", pa.int64()),
        ("sampled_days", pa.int64()),
        ("discovered_at", pa.timestamp("us", tz="UTC")),
    ]
)

# Securities that traded but are absent from the security master: the union of
# the discovery sweep and TaiwanStockDelisting, minus everything the master
# already has. `stock_name` and `delisted_date` are NULL for the codes only the
# sweep found — there is no upstream source of a name for those.

PROCESSED_DELISTED_SECURITY_SCHEMA = pa.schema(
    [
        ("stock_id", pa.string()),
        ("stock_name", pa.string()),
        ("delisted_date", pa.date32()),
        ("shape", pa.string()),
        ("first_seen", pa.date32()),
        ("last_seen", pa.date32()),
        ("source", pa.string()),
    ]
)

# ---------------------------------------------------------------------------
# trading calendar
# ---------------------------------------------------------------------------

RAW_TRADING_DATE_SCHEMA = pa.schema([("date", pa.string())])

PROCESSED_TRADING_DATE_SCHEMA = pa.schema(
    [
        ("date", pa.date32()),
        ("source", pa.string()),
    ]
)

RAW_SCHEMAS: dict[str, pa.Schema] = {
    "daily_prices": RAW_PRICE_SCHEMA,
    "adj_daily_prices": RAW_PRICE_SCHEMA,
    "tw_stock_info": RAW_STOCK_INFO_SCHEMA,
    "tw_industry": RAW_INDUSTRY_SCHEMA,
    "tw_trading_dates": RAW_TRADING_DATE_SCHEMA,
    "tw_delisting": RAW_DELISTING_SCHEMA,
    "discovered_universe": RAW_DISCOVERED_UNIVERSE_SCHEMA,
}


# ---------------------------------------------------------------------------
# coercion helpers
# ---------------------------------------------------------------------------


def clean_str(value: Any) -> str | None:
    """Normalise a JSON string field, mapping sentinel values to None."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in NONE_SENTINELS else text


def parse_date(value: Any) -> dt.date | None:
    """Parse ``YYYY-MM-DD`` tolerantly. Sentinels and junk become None.

    Never raises: a single unparseable row must not abort a whole day of data.
    """
    text = clean_str(value)
    if text is None:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_COERCERS = {
    pa.string(): clean_str,
    pa.int64(): _to_int,
    pa.float64(): _to_float,
}


def records_to_raw_table(records: list[dict[str, Any]], schema: pa.Schema) -> pa.Table:
    """Build a raw Parquet table from an API response.

    Columns absent from the response become all-NULL rather than raising, so a
    dataset that gains a field upstream does not break ingestion mid-run.
    """
    columns: dict[str, list[Any]] = {}
    for field in schema:
        coerce = _COERCERS.get(field.type, clean_str)
        columns[field.name] = [coerce(rec.get(field.name)) for rec in records]
    return pa.table(columns, schema=schema)


def enforce(table: pa.Table, schema: pa.Schema) -> pa.Table:
    """Reorder and cast ``table`` to exactly ``schema``.

    Raises if a required column is missing — that is a genuine contract breach
    and should stop the run rather than silently produce a NULL column.
    """
    missing = [f.name for f in schema if f.name not in table.column_names]
    if missing:
        raise ValueError(f"table is missing required columns {missing}")
    return table.select([f.name for f in schema]).cast(schema)
