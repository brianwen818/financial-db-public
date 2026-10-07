from __future__ import annotations

from tej_pipeline.processing import index_schema as IS
from tej_pipeline.processing import schema as S


def test_column_contract_is_10_wide():
    assert len(IS.COLUMNS) == 10
    assert len(IS.RAW_HEADERS) == 10
    # Three identity columns carry no TEJ field code; the rest map 1:1 onto the
    # field list in the workbook's own query definition.
    assert len(IS.TEJ_FIELD_CODES) == 7
    assert IS.TEJ_FIELD_CODES == (
        "IDXWE",
        "F_FLOAT",
        "CAPFAC",
        "ZSTK_AMT",
        "BASEN",
        "PRE_CLS",
        "MV%",
    )


def test_it_is_a_different_table_from_the_price_contract():
    """Sharing a module namespace must not blur the two TEJ queries together."""
    assert IS.TEJ_TABLE == "widxs"
    assert S.TEJ_TABLE == "waprcd1"
    assert set(IS.TEJ_FIELD_CODES).isdisjoint(S.TEJ_FIELD_CODES) is False  # MV% is in both
    assert IS.RAW_HEADERS != S.RAW_HEADERS[: len(IS.RAW_HEADERS)]


def test_processed_names_are_sql_safe_and_unique():
    names = [c[3] for c in IS.COLUMNS if c[3] not in IS.IDENTITY_NAMES]
    assert len(names) == len(set(names))
    for name in names:
        assert name.islower()
        assert name.replace("_", "").isalnum(), name


def test_processed_schema_matches_columns():
    # 5 identity/date + 7 data + 2 provenance
    assert len(IS.PROCESSED_COLUMNS) == 14
    assert IS.PROCESSED_COLUMNS[:5] == (
        "index_id",
        "index_name",
        "date",
        "stock_id",
        "stock_name",
    )
    assert IS.PROCESSED_COLUMNS[-2:] == ("source_file", "ingested_at")


def test_key_is_index_date_stock():
    """One row is one constituent of one index on one session."""
    assert IS.KEY_COLUMNS == ("index_id", "date", "stock_id")
    for name in IS.KEY_COLUMNS:
        assert name in IS.PROCESSED_COLUMNS


def test_every_index_directory_has_an_identity_string():
    """A seeded row must carry the exact string TEJ writes into column A."""
    assert set(IS.INDEX_IDENTITIES) == {"TWN50", "TM100"}
    for index_id, identity in IS.INDEX_IDENTITIES.items():
        # split_security is what the reader uses, so the identity has to round
        # trip through it back to the directory name.
        assert S.split_security(identity)[0] == index_id


def test_excel_columns_are_contiguous_from_a():
    assert IS.EXCEL_COLUMNS == tuple("ABCDEFGHIJ")
