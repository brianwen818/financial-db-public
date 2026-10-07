"""Inventory the raw workbooks without parsing their data.

This is the operational counterpart to the price data itself. Because a refresh
is driven by a GUI add-in rather than an HTTP call, it can fail in ways an API
client would never see: the add-in's session expires, a workbook is left open,
Excel is killed mid-save. None of those raise anything the pipeline can catch —
they just leave a workbook holding yesterday's numbers.

So every file's *identity* is recorded separately from its contents: when it was
last written, how many rows the add-in's own defined range claims, and which TEJ
query it was built from. Joined against the coverage of the processed layer
(``v_workbook_status``), that turns "did the refresh actually work?" into a SQL
question.

Everything here comes from the zip container and a few small XML parts, so
scanning all 120 workbooks costs well under a second.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import zipfile
from pathlib import Path

import pyarrow as pa

from tej_pipeline.processing import index_schema as IS
from tej_pipeline.processing import schema as S
from tej_pipeline.utils.parquet_io import atomic_write_table

log = logging.getLogger(__name__)

INVENTORY_SCHEMA = pa.schema(
    [
        pa.field("source_file", pa.string()),
        pa.field("stock_id", pa.string()),
        pa.field("file_bytes", pa.int64()),
        pa.field("modified_at", pa.timestamp("us", tz="UTC")),
        pa.field("sheet_name", pa.string()),
        pa.field("defined_name", pa.string()),
        pa.field("defined_range", pa.string()),
        # Rows the add-in's own block definition claims, header excluded.
        pa.field("declared_rows", pa.int64()),
        pa.field("tej_table", pa.string()),
        pa.field("tej_field_count", pa.int64()),
        pa.field("descending", pa.bool_()),
        pa.field("query_matches_contract", pa.bool_()),
        pa.field("scanned_at", pa.timestamp("us", tz="UTC")),
    ]
)

# The index workbooks are keyed by (index_id, year) rather than by a security,
# and there is no per-security status to join to, so they get their own
# inventory rather than NULL-padding the price one.
INDEX_INVENTORY_SCHEMA = pa.schema(
    [
        pa.field("source_file", pa.string()),
        pa.field("index_id", pa.string()),
        pa.field("year", pa.int64()),
        pa.field("file_bytes", pa.int64()),
        pa.field("modified_at", pa.timestamp("us", tz="UTC")),
        pa.field("sheet_name", pa.string()),
        pa.field("defined_name", pa.string()),
        pa.field("defined_range", pa.string()),
        pa.field("declared_rows", pa.int64()),
        pa.field("tej_table", pa.string()),
        pa.field("tej_field_count", pa.int64()),
        pa.field("descending", pa.bool_()),
        pa.field("query_matches_contract", pa.bool_()),
        pa.field("scanned_at", pa.timestamp("us", tz="UTC")),
    ]
)


_DEFINED = re.compile(r'<definedName name="([^"]+)">([^<]+)</definedName>')
_SHEET = re.compile(r'<sheet name="([^"]+)"')
_RANGE_END = re.compile(r"\$([A-Z]+)\$(\d+)\s*$")


def _comment_setting(comment_xml: str, key: str) -> str | None:
    m = re.search(rf"{key}=(.*?)###", comment_xml, re.S)
    return m.group(1).strip() if m else None


def _scan_container(
    path: Path,
    tej_table: str,
    field_codes: tuple[str, ...],
) -> dict[str, object]:
    """Read one workbook's identity from its zip container.

    ``tej_table``/``field_codes`` are the contract the file is expected to have
    been built from; the result records whether it matches rather than raising,
    because an off-contract file is an operational fact to surface in SQL, not
    a reason to abort a scan of the other workbooks.
    """
    path = Path(path)
    stat = path.stat()
    row: dict[str, object] = {
        "file_bytes": stat.st_size,
        "modified_at": dt.datetime.fromtimestamp(stat.st_mtime, dt.UTC),
        "sheet_name": None,
        "defined_name": None,
        "defined_range": None,
        "declared_rows": None,
        "tej_table": None,
        "tej_field_count": None,
        "descending": None,
        "query_matches_contract": False,
        "scanned_at": dt.datetime.now(dt.UTC),
    }

    with zipfile.ZipFile(path) as z:
        wb = z.read("xl/workbook.xml").decode("utf-8")
        sheets = _SHEET.findall(wb)
        row["sheet_name"] = sheets[0] if sheets else None

        defined = _DEFINED.findall(wb)
        if defined:
            name, ref = defined[0]
            row["defined_name"] = name
            row["defined_range"] = ref
            m = _RANGE_END.search(ref)
            if m:
                # The block includes the header row, which is not data.
                row["declared_rows"] = int(m.group(2)) - 1

        comments = [n for n in z.namelist() if n.startswith("xl/comments")]
        if comments:
            c = z.read(comments[0]).decode("utf-8")
            search = _comment_setting(c, "tSearchString")
            if search:
                parts = search.split("|||")
                if len(parts) >= 4:
                    row["tej_table"] = parts[2]
                    fields = [f for f in parts[3].split(",") if f]
                    row["tej_field_count"] = len(fields)
                    row["query_matches_contract"] = (
                        parts[2] == tej_table and tuple(fields) == field_codes
                    )
            desc = _comment_setting(c, "tDescending")
            if desc is not None:
                row["descending"] = desc.upper() == "Y"

    return row


def scan_workbook(path: Path) -> dict[str, object]:
    """Inventory row for one price workbook. Its identity is the filename."""
    path = Path(path)
    return {
        "source_file": path.name,
        "stock_id": path.stem,
        **_scan_container(path, S.TEJ_TABLE, S.TEJ_FIELD_CODES),
    }


def scan_index_workbook(path: Path, index_id: str) -> dict[str, object]:
    """Inventory row for one index-constituent workbook.

    Its identity is ``(index_id, year)``: the bare filename (``2026.xlsx``)
    repeats across indices, so it does not identify a workbook on its own.
    """
    path = Path(path)
    return {
        "source_file": f"{index_id}/{path.name}",
        "index_id": index_id,
        "year": int(path.stem),
        **_scan_container(path, IS.TEJ_TABLE, IS.TEJ_FIELD_CODES),
    }


def scan(raw_dir: Path, out_path: Path | None = None) -> pa.Table:
    """Scan every price workbook in ``raw_dir``; optionally write the inventory."""
    from tej_pipeline.processing.prices import assert_raw_readable, raw_workbooks

    assert_raw_readable(raw_dir)
    rows = [scan_workbook(p) for p in raw_workbooks(raw_dir)]
    table = pa.Table.from_pylist(rows, schema=INVENTORY_SCHEMA)

    off_contract = [
        r["source_file"] for r in rows if not r["query_matches_contract"]
    ]
    if off_contract:
        log.warning(
            "%d workbook(s) built from a different TEJ query: %s",
            len(off_contract),
            ", ".join(off_contract[:5]),
        )

    if out_path is not None:
        atomic_write_table(table, Path(out_path))
        log.info("wrote workbook inventory: %d rows -> %s", table.num_rows, out_path)
    return table


def scan_index(raw_dir: Path, out_path: Path | None = None) -> pa.Table:
    """Scan every index-constituent workbook; optionally write the inventory."""
    from tej_pipeline.processing.index_constituents import (
        assert_raw_readable,
        index_workbooks,
    )

    assert_raw_readable(raw_dir)
    rows = [scan_index_workbook(p, index_id) for index_id, p in index_workbooks(raw_dir)]
    table = pa.Table.from_pylist(rows, schema=INDEX_INVENTORY_SCHEMA)

    off_contract = [r["source_file"] for r in rows if not r["query_matches_contract"]]
    if off_contract:
        log.warning(
            "%d index workbook(s) built from a different TEJ query: %s",
            len(off_contract),
            ", ".join(off_contract[:5]),
        )

    if out_path is not None:
        atomic_write_table(table, Path(out_path))
        log.info("wrote index workbook inventory: %d rows -> %s", table.num_rows, out_path)
    return table
