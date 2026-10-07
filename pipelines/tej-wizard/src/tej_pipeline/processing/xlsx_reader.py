"""Read a TEJ Smart Wizard workbook without going through Excel.

The workbooks hold **static values only** -- verified across all 120 files, not
one contains a formula. So the sheet XML can be streamed directly, which is
both far faster than COM automation and safe to run while Excel is closed.

Three properties of the format that a naive reader gets wrong:

* **Empty cells are absent.** The add-in omits ``<c>`` entirely for a blank,
  so cells must be keyed off their ``r`` reference (``"AC1234"``). Reading them
  positionally silently shifts every column after the first NULL.
* **Dates are Excel serials**, not text. ``<v>36773</v>`` is 2000-09-04.
* **Strings are shared.** ``t="s"`` means ``<v>`` is an index into
  sharedStrings.xml, not a value.

The reader also refuses to guess: if the header row is not exactly the columns
the contract declares, it raises rather than producing a table that looks
plausible and is wrong.

Two contracts share this module. ``read_workbook`` parses the 35-column
``waprcd1`` price workbooks; ``read_index_workbook`` parses the 10-column
``widxs`` index-constituent workbooks. The container quirks above are properties
of the add-in's output, not of either query, so the row iterator is shared and
takes the column letters it should map.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import iterparse

import pyarrow as pa

from tej_pipeline.processing import index_schema as IS
from tej_pipeline.processing import schema as S

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_C = _NS + "c"
_V = _NS + "v"
_T = _NS + "t"
_ROW = _NS + "row"
_SI = _NS + "si"

# Position of each Excel column letter in the output tuple, per contract.
_COL_INDEX = {letter: i for i, letter in enumerate(S.EXCEL_COLUMNS)}
_INDEX_COL_INDEX = {letter: i for i, letter in enumerate(IS.EXCEL_COLUMNS)}


class WorkbookFormatError(ValueError):
    """The workbook is not the layout the requested contract expects."""


def _column_letter(ref: str) -> str:
    """``"AC1234"`` -> ``"AC"``. Hand-rolled because this runs ~23M times."""
    for i, ch in enumerate(ref):
        if ch.isdigit():
            return ref[:i]
    return ref


def _read_shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    out: list[str] = []
    with z.open("xl/sharedStrings.xml") as fh:
        for _, el in iterparse(fh, events=("end",)):
            if el.tag == _SI:
                # A shared string can be split across several <t> runs when it
                # carries formatting; join them or names come back truncated.
                out.append("".join(t.text or "" for t in el.iter(_T)))
                el.clear()
    return out


def iter_rows(
    path: Path, col_index: dict[str, int] | None = None
) -> Iterator[list[Any]]:
    """Yield each sheet row as a fixed-width list, absent cells as ``None``.

    ``col_index`` maps an Excel column letter to its slot; it defaults to the
    35-column price contract. Columns outside the map are skipped, so a wider
    sheet does not shift anything.
    """
    col_index = _COL_INDEX if col_index is None else col_index
    width = len(col_index)
    with zipfile.ZipFile(path) as z:
        shared = _read_shared_strings(z)
        with z.open("xl/worksheets/sheet1.xml") as fh:
            row: list[Any] = [None] * width
            for _, el in iterparse(fh, events=("end",)):
                tag = el.tag
                if tag == _C:
                    ref = el.get("r")
                    if ref is None:
                        continue
                    idx = col_index.get(_column_letter(ref))
                    if idx is None:
                        continue  # outside the contract's columns
                    v = el.find(_V)
                    if v is None or v.text is None:
                        continue
                    if el.get("t") == "s":
                        row[idx] = shared[int(v.text)]
                    else:
                        row[idx] = v.text
                elif tag == _ROW:
                    yield row
                    row = [None] * width
                    el.clear()


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    f = _to_float(v)
    return None if f is None else int(f)


def _check_header(
    path: Path,
    header: list[Any],
    expected: tuple[str, ...],
    excel_columns: tuple[str, ...],
) -> None:
    """Refuse a sheet whose header is not exactly the contract's columns."""
    got = tuple((h or "").strip() for h in header)
    if got == expected:
        return
    diff = [
        f"col {excel_columns[i]}: expected {e!r}, got {g!r}"
        for i, (e, g) in enumerate(zip(expected, got, strict=False))
        if e != g
    ]
    if len(got) != len(expected):
        diff.insert(0, f"width: expected {len(expected)} columns, got {len(got)}")
    raise WorkbookFormatError(
        f"{path.name}: unexpected header layout\n  " + "\n  ".join(diff[:5])
    )


def read_workbook(path: Path, ingested_at: dt.datetime | None = None) -> pa.Table:
    """Parse one workbook into the processed schema.

    Raises ``WorkbookFormatError`` if the header row is not the expected 35
    columns, or if the file holds more than one security -- both mean the
    workbook was built from a different TEJ query and must not be blended into
    the same partition set.
    """
    path = Path(path)
    ts = ingested_at or dt.datetime.now(dt.UTC)

    rows = iter_rows(path)
    try:
        header = next(rows)
    except StopIteration:
        raise WorkbookFormatError(f"{path.name}: empty sheet") from None

    _check_header(path, header, S.RAW_HEADERS, S.EXCEL_COLUMNS)

    cols: dict[str, list[Any]] = {name: [] for name in S.PROCESSED_COLUMNS}
    ids: set[str] = set()
    source_file = path.name

    for row in rows:
        if row[0] is None and row[1] is None:
            continue  # trailing blank row
        stock_id, stock_name = S.split_security(row[0])
        ids.add(stock_id)
        cols["stock_id"].append(stock_id)
        cols["stock_name"].append(stock_name)
        cols["date"].append(S.excel_serial_to_date(row[1]))
        for excel_col, _code, _zh, name in S.COLUMNS:
            if name in ("_security", "date"):
                continue
            raw = row[_COL_INDEX[excel_col]]
            if name in S.TEXT_COLUMNS:
                cols[name].append(raw if raw not in (None, "") else None)
            elif name in S.INT_COLUMNS:
                cols[name].append(_to_int(raw))
            else:
                cols[name].append(_to_float(raw))
        cols["source_file"].append(source_file)
        cols["ingested_at"].append(ts)

    if len(ids) > 1:
        raise WorkbookFormatError(
            f"{path.name}: expected one security per workbook, found {sorted(ids)}"
        )

    return pa.table(cols, schema=S.PROCESSED_SCHEMA)


def read_index_workbook(
    path: Path,
    index_id: str,
    ingested_at: dt.datetime | None = None,
) -> pa.Table:
    """Parse one ``widxs`` index-constituent workbook into the processed schema.

    ``index_id`` is the directory the workbook came from (``TWN50``), used both
    to build ``source_file`` and to check the sheet actually holds that index --
    a file filed under the wrong directory would otherwise be blended into the
    wrong membership history.

    Raises ``WorkbookFormatError`` if the header is not the expected 10 columns,
    if the sheet holds more than one index, or if it holds a different one.
    """
    path = Path(path)
    ts = ingested_at or dt.datetime.now(dt.UTC)

    rows = iter_rows(path, _INDEX_COL_INDEX)
    try:
        header = next(rows)
    except StopIteration:
        raise WorkbookFormatError(f"{path.name}: empty sheet") from None
    _check_header(path, header, IS.RAW_HEADERS, IS.EXCEL_COLUMNS)

    cols: dict[str, list[Any]] = {name: [] for name in IS.PROCESSED_COLUMNS}
    seen: set[str] = set()
    source_file = f"{index_id}/{path.name}"
    data_slots = [
        (name, _INDEX_COL_INDEX[excel_col])
        for excel_col, _code, _zh, name in IS.COLUMNS
        if name not in IS.IDENTITY_NAMES
    ]

    for row in rows:
        # A seeded row TEJ never populated carries an index and a date but no
        # constituent. It is a prompt, not an observation, so it is dropped
        # rather than becoming a member row with a NULL stock_id.
        if row[0] is None or row[1] is None or row[2] is None:
            continue
        found_index, index_name = S.split_security(row[0])
        seen.add(found_index)
        stock_id, stock_name = S.split_security(row[2])
        cols["index_id"].append(found_index)
        cols["index_name"].append(index_name)
        cols["date"].append(S.excel_serial_to_date(row[1]))
        cols["stock_id"].append(stock_id)
        cols["stock_name"].append(stock_name)
        for name, slot in data_slots:
            cols[name].append(_to_float(row[slot]))
        cols["source_file"].append(source_file)
        cols["ingested_at"].append(ts)

    if len(seen) > 1:
        raise WorkbookFormatError(
            f"{source_file}: expected one index per workbook, found {sorted(seen)}"
        )
    if seen and index_id not in seen:
        raise WorkbookFormatError(
            f"{source_file}: filed under {index_id!r} but holds {sorted(seen)}"
        )

    return pa.table(cols, schema=IS.PROCESSED_SCHEMA)
