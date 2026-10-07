"""Canonical schema for the daily index-constituent dataset. Source of truth.

Every workbook in ``raw/daily-index-constituents/<INDEX>/<YYYY>.xlsx`` is one
query against the TEJ table ``widxs`` (指數成分股) at DAY frequency, 10 columns
wide, one index-year per file. The query definition lives in the A1 cell
comment; ``TEJ_FIELD_CODES`` below is that definition's field list, in order.

This sits beside :mod:`tej_pipeline.processing.schema`, which describes the
``waprcd1`` price contract. The two share the Excel-serial and identity-splitting
helpers and nothing else -- different table, different grain, different units.

Facts encoded here, all measured against the 48 workbooks on 2026-09-05:

* **The grain is index x day x constituent**, not one security per file.
  Column A is the *index* (``"TWN50 台灣50指數"``), column C is the constituent
  (``"1216 統一"``). Both are ``"<id> <name>"`` in a single cell.
* **The date is the effective date and the data is the previous day's.**
  ``前日調整收盤價`` on the 2026-09-07 rows is 2026-09-04's close, so a file
  refreshed on a Friday already carries Monday's constituent list. This dataset
  therefore runs one session *ahead* of the price dataset, and a horizon of
  "today" would leave the newest row permanently unfetched.
* **The constituent count is not fixed.** Measured per-day counts are 46-51 for
  TWN50 (49 members in 2002, 51 on two days in 2010 and one in 2021) and 98-102
  for TM100. Any assertion of exactly 50 or 100 is wrong.
* **``股數`` is in shares, not thousands.** 1101 carries 7,523,181,742 here
  against ``shares_outstanding_k`` = 7,523,182 in the price table. The two
  columns are the same quantity on scales 1,000 apart.
* ``(index_id, date, stock_id)`` is unique across all 832,648 rows, and the
  year files do not overlap: no date appears in two files of the same index.
"""

from __future__ import annotations

import pyarrow as pa

# The dataset's own name for itself, from tSearchString in the A1 comment.
TEJ_TABLE = "widxs"

# The indices collected, keyed by the directory name under the raw root. The
# value is the exact identity string TEJ writes into column A, which is also
# what a seeded row must carry for the add-in to resolve the query.
INDEX_IDENTITIES: dict[str, str] = {
    "TWN50": "TWN50 台灣50指數",
    "TM100": "TM100 台灣中型指數",
}

# Column order as the add-in writes it, A..J. Each entry is
# (excel_column, tej_field_code, chinese_header, processed_name).
#
# tej_field_code is None for the three identity columns, which the add-in always
# emits and which do not appear in the query's field list.
COLUMNS: tuple[tuple[str, str | None, str, str], ...] = (
    ("A", None, "公司代碼", "_index"),  # split into index_id + index_name
    ("B", None, "年月日", "date"),
    ("C", None, "成份股", "_constituent"),  # split into stock_id + stock_name
    ("D", "IDXWE", "指數因子", "index_factor"),
    ("E", "F_FLOAT", "公眾流通係數", "free_float_factor"),
    ("F", "CAPFAC", "比重上限因子", "cap_factor"),
    ("G", "ZSTK_AMT", "股數", "shares"),
    ("H", "BASEN", "指數基值", "index_base_value"),
    ("I", "PRE_CLS", "前日調整收盤價", "prev_adj_close"),
    ("J", "MV%", "前日市值比重", "prev_weight_pct"),
)

RAW_HEADERS: tuple[str, ...] = tuple(c[2] for c in COLUMNS)
EXCEL_COLUMNS: tuple[str, ...] = tuple(c[0] for c in COLUMNS)
TEJ_FIELD_CODES: tuple[str, ...] = tuple(c[1] for c in COLUMNS if c[1] is not None)

# Identity columns, handled by the reader rather than mapped straight through.
IDENTITY_NAMES = frozenset({"_index", "_constituent", "date"})

# Everything TEJ delivers here is numeric; there is no text field beyond the
# two identity cells, and no integer count of the kind `transactions` is in the
# price contract.
DATA_NAMES: tuple[str, ...] = tuple(
    c[3] for c in COLUMNS if c[3] not in IDENTITY_NAMES
)

PROCESSED_SCHEMA = pa.schema(
    [
        pa.field("index_id", pa.string()),
        pa.field("index_name", pa.string()),
        pa.field("date", pa.date32()),
        pa.field("stock_id", pa.string()),
        pa.field("stock_name", pa.string()),
        *[pa.field(name, pa.float64()) for name in DATA_NAMES],
        # Provenance, mirroring the price pipeline's two lineage columns.
        # source_file is "<INDEX>/<YYYY>.xlsx" -- the bare filename repeats
        # across indices, so it alone would not identify a workbook.
        pa.field("source_file", pa.string()),
        pa.field("ingested_at", pa.timestamp("us", tz="UTC")),
    ]
)

PROCESSED_COLUMNS: tuple[str, ...] = tuple(PROCESSED_SCHEMA.names)

# The natural key. Enforced on every rebuild: a duplicate means one date was
# collected into two year files, which would double-weight that day.
KEY_COLUMNS: tuple[str, ...] = ("index_id", "date", "stock_id")
