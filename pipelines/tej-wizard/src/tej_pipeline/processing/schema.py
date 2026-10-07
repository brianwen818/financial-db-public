"""Canonical schema and column mapping. The single source of truth.

Every TEJ workbook in ``raw/adj-daily-prices-ticker`` is one query against the
TEJ table ``waprcd1`` (調整後日行情) at DAY frequency, 35 columns wide, one
security per file. The query definition is stored by the add-in in the A1 cell
comment; ``TEJ_FIELD_CODES`` below is that definition's field list, in order.

Two families, mirroring the FinMind pipeline:

**raw** is the workbook as delivered — Chinese headers, Excel serial dates,
``證券代碼`` holding ``"2330 台積電"`` as one string. Nothing is interpreted.

**processed** is the query contract: real ``DATE``, SQL-safe snake_case names,
id and name split apart. DuckDB views read only this layer.

Facts encoded here, all measured against the 120 workbooks on 2026-09-03:

* Dates arrive as **Excel serials** (36773 = 2000-09-04, 46267 = 2026-09-02),
  not strings. The 1900 leap-year bug does not bite: the earliest serial in the
  data is 36773, far above the serial-60 boundary where the bug applies.
* ``證券代碼`` is ``"<id> <name>"`` in a single cell. The id keeps leading
  zeros (``0050``) and is never numeric.
* Empty cells are **omitted from the XML entirely**, not written as blanks, so
  a reader must map cells by their ``r`` reference rather than by position.
* **Only OHLC are back-adjusted.** ``最後揭示買價/賣價`` and the three
  next-day reference prices are raw quotes. On 2000-09-08 2330 closes at 26.10
  adjusted while ``次日開盤參考價`` reads 124.5. Mixing them silently is the
  single easiest way to get a wrong backtest out of this dataset.
"""

from __future__ import annotations

import datetime as dt

import pyarrow as pa

# The dataset's own name for itself, from tSearchString in the A1 comment.
TEJ_TABLE = "waprcd1"

# Excel's day-zero. Serial 36773 -> 2000-09-04.
EXCEL_EPOCH = dt.date(1899, 12, 30)

# Column order as the add-in writes it, A..AI. Each entry is
# (excel_column, tej_field_code, chinese_header, processed_name).
#
# tej_field_code is None for the two identity columns, which the add-in always
# emits and which do not appear in the query's field list.
COLUMNS: tuple[tuple[str, str | None, str, str], ...] = (
    ("A", None, "證券代碼", "_security"),  # split into stock_id + stock_name
    ("B", None, "年月日", "date"),
    ("C", "OPEN", "開盤價(元)", "open"),
    ("D", "HIGH", "最高價(元)", "high"),
    ("E", "LOW", "最低價(元)", "low"),
    ("F", "CLOSE", "收盤價(元)", "close"),
    ("G", "VOLUME", "成交量(千股)", "volume_k_shares"),
    ("H", "AMOUNT", "成交值(千元)", "turnover_k"),
    ("I", "ROI", "報酬率％", "return_pct"),
    ("J", "TURNOVER", "週轉率％", "turnover_pct"),
    ("K", "OUTSTANDING", "流通在外股數(千股)", "shares_outstanding_k"),
    ("L", "MV", "市值(百萬元)", "market_cap_m"),
    ("M", "BID", "最後揭示買價", "bid"),
    ("N", "OFFER", "最後揭示賣價", "offer"),
    ("O", "ROIB", "報酬率-Ln", "return_ln"),
    ("P", "MV%", "市值比重％", "market_cap_weight_pct"),
    ("Q", "AMT%", "成交值比重％", "turnover_weight_pct"),
    ("R", "TRN_D", "成交筆數(筆)", "transactions"),
    ("S", "PER-TSE", "本益比-TSE", "pe_tse"),
    ("T", "PER-TEJ", "本益比-TEJ", "pe_tej"),
    ("U", "PBR-TSE", "股價淨值比-TSE", "pbr_tse"),
    ("V", "PBR-TEJ", "股價淨值比-TEJ", "pbr_tej"),
    ("W", "LIMIT", "漲跌停", "limit_flag"),
    ("X", "TEJ_PSR", "股價營收比-TEJ", "psr_tej"),
    ("Y", "DIV_YID", "股利殖利率-TSE", "div_yield_tse"),
    ("Z", "TEJ_CDIV", "現金股利率", "cash_div_yield"),
    ("AA", "CLSCHG", "股價漲跌(元)", "price_change"),
    ("AB", "HMLPCT", "高低價差%", "high_low_spread_pct"),
    ("AC", "REFPRC", "次日開盤參考價", "next_ref_price"),
    ("AD", "U_LIMIT", "次日漲停價", "next_limit_up"),
    ("AE", "D_LIMIT", "次日跌停價", "next_limit_down"),
    ("AF", "XATTN1", "注意股票(A)", "attention_flag"),
    ("AG", "XATTN2", "處置股票(D)", "disposition_flag"),
    ("AH", "XSTAT1", "全額交割(Y)", "full_delivery_flag"),
    ("AI", "PMKT", "市場別", "market"),
)

RAW_HEADERS: tuple[str, ...] = tuple(c[2] for c in COLUMNS)
EXCEL_COLUMNS: tuple[str, ...] = tuple(c[0] for c in COLUMNS)
TEJ_FIELD_CODES: tuple[str, ...] = tuple(c[1] for c in COLUMNS if c[1] is not None)

# Columns that are text in the source. Everything else numeric is cast to
# double; `transactions` is the sole integer count and is handled separately.
TEXT_COLUMNS = frozenset(
    {"limit_flag", "attention_flag", "disposition_flag", "full_delivery_flag", "market"}
)
INT_COLUMNS = frozenset({"transactions"})

# Back-adjusted price columns, versus the raw quotes that sit beside them.
# Documented rather than enforced -- both are legitimately useful, but they are
# on different scales and must never be compared or combined.
ADJUSTED_PRICE_COLUMNS = frozenset({"open", "high", "low", "close"})
UNADJUSTED_PRICE_COLUMNS = frozenset(
    {"bid", "offer", "next_ref_price", "next_limit_up", "next_limit_down"}
)


def _field(name: str) -> pa.Field:
    if name in TEXT_COLUMNS:
        return pa.field(name, pa.string())
    if name in INT_COLUMNS:
        return pa.field(name, pa.int64())
    return pa.field(name, pa.float64())


PROCESSED_SCHEMA = pa.schema(
    [
        pa.field("stock_id", pa.string()),
        pa.field("stock_name", pa.string()),
        pa.field("date", pa.date32()),
        *[_field(c[3]) for c in COLUMNS if c[3] not in ("_security", "date")],
        # Provenance, mirroring the FinMind pipeline's two lineage columns.
        pa.field("source_file", pa.string()),
        pa.field("ingested_at", pa.timestamp("us", tz="UTC")),
    ]
)

PROCESSED_COLUMNS: tuple[str, ...] = tuple(PROCESSED_SCHEMA.names)


def excel_serial_to_date(serial: float | int | str) -> dt.date:
    """Convert an Excel 1900-system serial to a real date.

    The 1900 leap-year bug shifts serials <= 60 by one day. This dataset starts
    at serial 36773 (2000-09-04), so the simple offset is exact throughout; a
    serial in the buggy range means the file is not what we think it is.
    """
    n = int(float(serial))
    if n <= 60:
        raise ValueError(
            f"Excel serial {n} falls in the 1900 leap-bug range; "
            "this dataset starts at 36773 (2000-09-04), so the input is suspect"
        )
    return EXCEL_EPOCH + dt.timedelta(days=n)


def split_security(cell: str) -> tuple[str, str | None]:
    """Split ``"2330 台積電"`` into ``("2330", "台積電")``.

    The id is everything up to the first space. Names containing spaces keep
    them; an id with no name at all yields ``None`` rather than an empty string
    so the distinction survives into SQL.
    """
    text = (cell or "").strip()
    if not text:
        raise ValueError("empty 證券代碼 cell")
    stock_id, _, name = text.partition(" ")
    name = name.strip()
    return stock_id, name or None
