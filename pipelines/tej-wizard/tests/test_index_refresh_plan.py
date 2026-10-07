from __future__ import annotations

import datetime as dt

import pytest
from conftest import write_index_sheet

from tej_pipeline.ingestion.excel_refresh import INDEX_SEED, PRICE_SEED
from tej_pipeline.ingestion.trading_dates import (
    index_identity,
    latest_workbook_date,
    next_session,
    plan_index_seeds,
)
from tej_pipeline.processing import index_schema as IS
from tej_pipeline.processing.schema import EXCEL_EPOCH
from tej_pipeline.processing.xlsx_reader import _INDEX_COL_INDEX

# A week of sessions around the measured 2026-09-05 state: Friday the 4th
# settled, Monday the 7th already published by TEJ.
CALENDAR = (
    dt.date(2026, 9, 2),
    dt.date(2026, 9, 3),
    dt.date(2026, 9, 4),
    dt.date(2026, 9, 7),
    dt.date(2026, 9, 8),
)


def make_workbook(tmp_path, index_id: str, year: int, newest: dt.date):
    """A one-row index workbook, newest-first like the real ones."""
    directory = tmp_path / index_id
    directory.mkdir(exist_ok=True)
    path = directory / f"{year}.xlsx"
    serial = (newest - EXCEL_EPOCH).days
    write_index_sheet(
        path,
        [
            list(IS.RAW_HEADERS),
            [
                IS.INDEX_IDENTITIES[index_id],
                str(serial),
                "2330 台積電",
                "0.01",
                "0.9",
                "1",
                "1",
                "1",
                "100",
                "100",
            ],
        ],
    )
    return path



def test_next_session_is_strictly_after():
    assert next_session(CALENDAR, dt.date(2026, 9, 5)) == dt.date(2026, 9, 7)
    # On a trading day, the next session is tomorrow, not today.
    assert next_session(CALENDAR, dt.date(2026, 9, 4)) == dt.date(2026, 9, 7)
    assert next_session(CALENDAR, dt.date(2026, 12, 31)) is None


def test_latest_workbook_date_reads_the_index_contract(tmp_path):
    path = make_workbook(tmp_path, "TWN50", 2026, dt.date(2026, 9, 4))
    assert latest_workbook_date(path, _INDEX_COL_INDEX) == dt.date(2026, 9, 4)


def test_future_sessions_are_optional_settled_ones_are_required(tmp_path):
    """The horizon is the next session, which by definition has not happened."""
    path = make_workbook(tmp_path, "TWN50", 2026, dt.date(2026, 9, 3))
    (plan,) = plan_index_seeds({"TWN50": path}, CALENDAR, dt.date(2026, 9, 5))
    assert plan.workbook_latest == dt.date(2026, 9, 3)
    # 09-04 has settled; 09-07 is the next session and may not be published yet.
    assert plan.required == (dt.date(2026, 9, 4),)
    assert plan.optional == (dt.date(2026, 9, 7),)
    assert plan.seed_dates == (dt.date(2026, 9, 4), dt.date(2026, 9, 7))


def test_nothing_to_seed_once_the_workbook_reaches_the_horizon(tmp_path):
    path = make_workbook(tmp_path, "TWN50", 2026, dt.date(2026, 9, 7))
    (plan,) = plan_index_seeds({"TWN50": path}, CALENDAR, dt.date(2026, 9, 5))
    assert plan.seed_dates == ()


def test_the_horizon_never_crosses_into_another_year(tmp_path):
    """Seeding January into last year's workbook would make two files serve
    one session, which the rebuild refuses to ingest."""
    calendar = (dt.date(2026, 12, 30), dt.date(2026, 12, 31), dt.date(2027, 1, 4))
    path = make_workbook(tmp_path, "TWN50", 2026, dt.date(2026, 12, 30))
    (plan,) = plan_index_seeds({"TWN50": path}, calendar, dt.date(2026, 12, 31))
    assert plan.required == (dt.date(2026, 12, 31),)
    assert plan.optional == ()  # 2027-01-04 belongs to 2027.xlsx, which does not exist yet


def test_plans_cover_every_index_given(tmp_path):
    paths = {
        "TWN50": make_workbook(tmp_path, "TWN50", 2026, dt.date(2026, 9, 3)),
        "TM100": make_workbook(tmp_path, "TM100", 2026, dt.date(2026, 9, 4)),
    }
    plans = plan_index_seeds(paths, CALENDAR, dt.date(2026, 9, 5))
    assert [p.index_id for p in plans] == ["TM100", "TWN50"]
    assert len(plans[0].seed_dates) == 1  # TM100 only needs 09-07
    assert len(plans[1].seed_dates) == 2


def test_identity_is_the_exact_string_tej_writes():
    assert index_identity("TWN50") == "TWN50 台灣50指數"
    with pytest.raises(KeyError, match="unknown index"):
        index_identity("TWN100")


def test_seed_contracts_point_at_different_proof_columns():
    """The price contract proves a row with `close`; the index one with 成份股.

    Getting this wrong would silently accept a row TEJ never filled, because
    the price contract's column F is empty on an unfilled index row too.
    """
    assert PRICE_SEED.populated_offset == 4  # column F, close
    assert INDEX_SEED.populated_offset == 1  # column C, 成份股
    # One seeded index date expands into a whole constituent set, so the scan
    # window has to be far wider than one row per date.
    assert PRICE_SEED.max_rows_per_date == 1
    assert INDEX_SEED.max_rows_per_date >= 102  # the measured TM100 maximum


def test_single_cell_ranges_are_flattened():
    """Excel returns a scalar for a one-cell range and a 2-D tuple otherwise.

    A security with exactly one trading day would otherwise make the validator
    iterate over the characters of its own id.
    """
    from tej_pipeline.ingestion.excel_refresh import _column_values

    assert _column_values((("2330",), ("2454",))) == ("2330", "2454")
    assert _column_values("2330") == ("2330",)
    assert _column_values(46269.0) == (46269.0,)
    assert _column_values(None) == ()


def test_no_new_data_is_not_a_failure():
    """A security that stopped trading returns nothing for every seeded date.

    2867 三商壽 did exactly that on 2026-09-05 (last bar 2026-08-31) and
    aborted the whole daily run. The result carries a flag so the caller can
    tell that apart from a workbook that failed to refresh.
    """
    from tej_pipeline.ingestion.excel_refresh import RefreshResult

    stopped = RefreshResult(
        stock_id="2867",
        path=__import__("pathlib").Path("2867.xlsx"),
        ok=True,
        seconds=1.0,
        no_new_data=True,
        message="TEJ has no data for 2026-09-04; workbook left unchanged",
    )
    assert stopped.ok
    # The file was not written, so nothing changed and the run is not a failure.
    assert not stopped.changed
    assert stopped.no_new_data
