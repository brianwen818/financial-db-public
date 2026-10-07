from __future__ import annotations

import datetime as dt
import shutil
import zipfile

import pyarrow.compute as pc
import pytest
from conftest import make_index_table, write_index_sheet

from tej_pipeline.processing import index_schema as IS
from tej_pipeline.processing.index_constituents import (
    RawLayerBusyError,
    assert_raw_readable,
    assert_unique_key,
    assert_years_do_not_overlap,
    current_year_workbooks,
    index_workbooks,
    workbook_year,
)
from tej_pipeline.processing.xlsx_reader import WorkbookFormatError, read_index_workbook


# --- reading a real workbook -------------------------------------------------


def test_reads_a_real_index_workbook(sample_index_workbook):
    index_id, path = sample_index_workbook
    table = read_index_workbook(path, index_id)
    assert table.num_rows > 0
    assert table.schema.equals(IS.PROCESSED_SCHEMA)
    ids = pc.unique(table.column("index_id").combine_chunks()).to_pylist()
    assert ids == [index_id]


def test_source_file_carries_the_index_directory(sample_index_workbook):
    """``2026.xlsx`` repeats across indices, so the bare name is not an id."""
    index_id, path = sample_index_workbook
    table = read_index_workbook(path, index_id)
    assert table.column("source_file")[0].as_py() == f"{index_id}/{path.name}"


def test_dates_are_real_dates_and_stay_inside_the_file_year(sample_index_workbook):
    index_id, path = sample_index_workbook
    table = read_index_workbook(path, index_id)
    dates = table.column("date").to_pylist()
    assert all(isinstance(d, dt.date) for d in dates)
    # A year workbook holds that year and nothing else; a stray date from a
    # neighbouring year is how two workbooks would come to serve one session.
    assert {d.year for d in dates} == {workbook_year(path)}


def test_constituents_are_split_from_the_index(sample_index_workbook):
    """Column A is the index, column C is the member. Confusing them is silent."""
    index_id, path = sample_index_workbook
    table = read_index_workbook(path, index_id)
    stock_ids = set(pc.unique(table.column("stock_id").combine_chunks()).to_pylist())
    assert index_id not in stock_ids
    assert len(stock_ids) > 10
    assert all(sid and not sid.startswith(index_id) for sid in stock_ids)


def test_rejects_a_workbook_filed_under_the_wrong_index(sample_index_workbook):
    """A TM100 file in the TWN50 directory would corrupt both histories."""
    index_id, path = sample_index_workbook
    other = "TWN50" if index_id != "TWN50" else "TM100"
    with pytest.raises(WorkbookFormatError, match="filed under"):
        read_index_workbook(path, other)


def test_rejects_the_price_contract(raw_dir, index_raw_dir):
    """The 35-column price workbook must not parse as a 10-column index one."""
    price_books = sorted(raw_dir.glob("*.xlsx"))
    if not price_books:
        pytest.skip("no price workbooks present")
    with pytest.raises(WorkbookFormatError, match="header layout"):
        read_index_workbook(price_books[0], "TWN50")


def test_rejects_a_workbook_with_the_wrong_header(tmp_path, sample_index_workbook):
    index_id, path = sample_index_workbook
    broken = tmp_path / "2099.xlsx"
    shutil.copy2(path, broken)

    with zipfile.ZipFile(broken) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    parts["xl/sharedStrings.xml"] = parts["xl/sharedStrings.xml"].replace(
        "<t>成份股</t>".encode(), "<t>證券代碼</t>".encode()
    )
    with zipfile.ZipFile(broken, "w") as z:
        for name, data in parts.items():
            z.writestr(name, data)

    with pytest.raises(WorkbookFormatError, match="header layout"):
        read_index_workbook(broken, index_id)


def test_ingested_at_is_stamped(sample_index_workbook):
    index_id, path = sample_index_workbook
    ts = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    table = read_index_workbook(path, index_id, ingested_at=ts)
    assert table.column("ingested_at")[0].as_py() == ts


# --- workbook discovery ------------------------------------------------------


def test_only_year_files_are_ingested(tmp_path):
    """A stray copy would duplicate every session it covers."""
    (tmp_path / "TWN50").mkdir()
    for name in ("2025.xlsx", "2026.xlsx", "26-test.xlsx", "~$2026.xlsx", "notes.xlsx"):
        (tmp_path / "TWN50" / name).write_bytes(b"x")
    found = [p.name for _, p in index_workbooks(tmp_path)]
    assert found == ["2025.xlsx", "2026.xlsx"]


def test_index_id_comes_from_the_directory(tmp_path):
    for index_id in ("TM100", "TWN50"):
        (tmp_path / index_id).mkdir()
        (tmp_path / index_id / "2026.xlsx").write_bytes(b"x")
    assert [i for i, _ in index_workbooks(tmp_path)] == ["TM100", "TWN50"]


def test_workbook_year_rejects_a_non_year_name(tmp_path):
    with pytest.raises(ValueError, match="not a <YYYY>"):
        workbook_year(tmp_path / "26-test.xlsx")


def test_current_year_workbooks_is_what_a_refresh_touches(tmp_path):
    for index_id in ("TWN50", "TM100"):
        (tmp_path / index_id).mkdir()
        for year in (2025, 2026):
            (tmp_path / index_id / f"{year}.xlsx").write_bytes(b"x")
    found = current_year_workbooks(tmp_path, 2026)
    assert set(found) == {"TWN50", "TM100"}
    assert all(p.name == "2026.xlsx" for p in found.values())
    # A year with no workbook yet returns nothing rather than falling back to
    # the newest one -- the caller has to notice and say so.
    assert current_year_workbooks(tmp_path, 2027) == {}


def test_raw_layer_refuses_to_read_while_excel_holds_a_workbook(tmp_path):
    (tmp_path / "TWN50").mkdir()
    (tmp_path / "TWN50" / "2026.xlsx").write_bytes(b"x")
    assert_raw_readable(tmp_path)  # no lock file yet

    # The lock sits beside the workbook, one level down from the raw root.
    (tmp_path / "TWN50" / "~$2026.xlsx").write_bytes(b"lock")
    with pytest.raises(RawLayerBusyError, match="TWN50/2026.xlsx"):
        assert_raw_readable(tmp_path)


# --- key and overlap invariants ---------------------------------------------


def test_unique_key_accepts_the_same_stock_in_both_indices():
    """95 securities have been in TWN50 and TM100 at different times."""
    assert_unique_key(
        make_index_table(
            [
                ("TWN50", dt.date(2024, 1, 2), "2330"),
                ("TM100", dt.date(2024, 1, 2), "2330"),
                ("TWN50", dt.date(2024, 1, 3), "2330"),
            ]
        )
    )


def test_unique_key_rejects_a_repeated_constituent():
    dup = make_index_table(
        [
            ("TWN50", dt.date(2024, 1, 2), "2330"),
            ("TWN50", dt.date(2024, 1, 2), "2330"),
        ]
    )
    with pytest.raises(ValueError, match="not unique"):
        assert_unique_key(dup)


def test_overlapping_year_workbooks_are_rejected():
    """Two files serving one session differ only in provenance, so the key
    check cannot see it."""
    table = make_index_table([("TWN50", dt.date(2024, 1, 2), "2330")])
    other = make_index_table([("TWN50", dt.date(2024, 1, 2), "2454")])
    other = other.set_column(
        other.schema.get_field_index("source_file"),
        "source_file",
        [["TWN50/2023.xlsx"]],
    )
    import pyarrow as pa

    with pytest.raises(ValueError, match="must not overlap"):
        assert_years_do_not_overlap(pa.concat_tables([table, other]))


def test_non_overlapping_years_pass():
    assert_years_do_not_overlap(
        make_index_table(
            [
                ("TWN50", dt.date(2023, 12, 29), "2330"),
                ("TWN50", dt.date(2024, 1, 2), "2330"),
            ]
        )
    )


# --- seeded rows -------------------------------------------------------------

def test_a_seeded_row_tej_never_filled_is_dropped(tmp_path):
    """Seeding writes only columns A and B; TEJ fills the rest.

    If the refresh does not populate one, the prompt row must not survive into
    the processed layer as a member with a NULL stock_id -- it would land in
    every cross-section for that session.
    """
    path = tmp_path / "2026.xlsx"
    identity = IS.INDEX_IDENTITIES["TWN50"]
    write_index_sheet(
        path,
        [
            list(IS.RAW_HEADERS),
            [identity, "46272", None, None, None, None, None, None, None, None],
            [identity, "46269", "2330 台積電", "0.01", "0.9", "1", "1", "1", "100", "30"],
        ],
    )
    table = read_index_workbook(path, "TWN50")
    assert table.num_rows == 1
    assert table.column("stock_id").to_pylist() == ["2330"]
    assert table.column("date").to_pylist() == [dt.date(2026, 9, 4)]


def test_a_filled_seeded_row_is_kept(tmp_path):
    """The same shape once TEJ has populated it is ordinary data."""
    path = tmp_path / "2026.xlsx"
    identity = IS.INDEX_IDENTITIES["TWN50"]
    write_index_sheet(
        path,
        [
            list(IS.RAW_HEADERS),
            [identity, "46272", "2330 台積電", "0.01", "0.9", "1", "1", "1", "100", "30"],
        ],
    )
    table = read_index_workbook(path, "TWN50")
    assert table.num_rows == 1
    assert table.column("date").to_pylist() == [dt.date(2026, 9, 7)]
    assert table.column("prev_weight_pct").to_pylist() == [30.0]
