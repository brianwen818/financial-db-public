"""End-to-end checks against the built warehouse.

These are the assertions that matter after a backfill: the DuckDB views answer,
the scope is exactly the security-master universe, and no warrant leaked in.
Everything skips cleanly if the warehouse has not been built yet.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest


@pytest.fixture(scope="module")
def con(settings):
    if not settings.duckdb_path.exists():
        pytest.skip("finmind.duckdb not built; run scripts/build_db.py")
    connection = duckdb.connect(str(settings.duckdb_path), read_only=True)
    yield connection
    connection.close()


def _scalar(con, sql):
    row = con.execute(sql).fetchone()
    return row[0] if row else None


def _has_rows(con, view) -> bool:
    try:
        return _scalar(con, f"SELECT count(*) FROM {view}") > 0
    except duckdb.Error:
        return False


class TestViewsAnswer:
    VIEWS = [
        "v_daily_prices",
        "v_adj_daily_prices",
        "v_stock_info_history",
        "v_stock_info_latest",
        "v_securities",
        "v_securities_all",
        "v_delisted_securities",
        "v_indices",
        "v_industry",
        "v_industry_by_stock",
        "v_trading_calendar",
        "v_daily_coverage",
        "v_prices_combined",
        "v_ingestion_runs",
    ]

    @pytest.mark.parametrize("view", VIEWS)
    def test_view_is_queryable(self, con, view):
        assert _scalar(con, f"SELECT count(*) FROM {view}") is not None


class TestSchemaContract:
    def test_price_columns_and_types(self, con):
        cols = {r[0]: r[1] for r in con.execute("DESCRIBE SELECT * FROM v_daily_prices").fetchall()}
        assert cols["date"] == "DATE"
        assert cols["stock_id"] == "VARCHAR"
        for c in ("volume", "turnover_value", "transactions"):
            assert cols[c] == "BIGINT", f"{c} must be BIGINT to hold 5.3e10"
        assert {"high", "low"} <= cols.keys()
        assert not ({"max", "min"} & cols.keys())

    def test_leading_zero_ids_survive(self, con):
        if not _has_rows(con, "v_daily_prices"):
            pytest.skip("no price data yet")
        assert _scalar(con, "SELECT count(*) FROM v_daily_prices WHERE stock_id = '0050'") > 0


class TestStockInfoQuirks:
    def test_exactly_32_null_snapshot_dates(self, con):
        """The literal "None" strings must have become real NULLs."""
        assert (
            _scalar(con, "SELECT count(*) FROM v_stock_info_history WHERE snapshot_date IS NULL")
            == 32
        )

    def test_index_flag_matches_null_dates(self, con):
        """Index pseudo-tickers are exactly the rows FinMind sends "None" for."""
        assert _scalar(con, "SELECT count(*) FROM v_indices") == 32

    def test_securities_exclude_indices(self, con):
        total = _scalar(con, "SELECT count(*) FROM v_stock_info_latest")
        securities = _scalar(con, "SELECT count(*) FROM v_securities")
        assert securities == total - 32

    def test_industry_normalisation_applied(self, con):
        """The upstream 創新版/創新板 typo and the X/X類 pairs must be folded."""
        assert (
            _scalar(
                con,
                "SELECT count(*) FROM v_stock_info_history "
                "WHERE industry_category <> industry_category_norm",
            )
            > 0
        )
        assert (
            _scalar(
                con,
                "SELECT count(*) FROM v_stock_info_history "
                "WHERE industry_category_norm = '創新版股票'",
            )
            == 0
        )


class TestScope:
    def test_no_warrants_leaked_in(self, con):
        """Prices must cover only the known universe.

        The upstream whole-market endpoint returns ~43,700 securities per day,
        ~40,900 of them warrants and TDRs that are deliberately out of scope.

        Checked against v_securities_all rather than v_stock_info_latest: the
        master is current-state, so a delisted security is legitimately in the
        prices and legitimately absent from it. Asserting against the master
        alone is what would force the warehouse to hold survivors only.
        """
        if not _has_rows(con, "v_daily_prices"):
            pytest.skip("no price data yet")
        stray = _scalar(
            con,
            "SELECT count(DISTINCT stock_id) FROM v_daily_prices p "
            "WHERE NOT EXISTS (SELECT 1 FROM v_securities_all s "
            "                  WHERE s.stock_id = p.stock_id)",
        )
        assert stray == 0, f"{stray} securities in prices are in neither master nor delisted list"

    def test_adj_is_subset_of_unadjusted(self, con):
        if not (_has_rows(con, "v_adj_daily_prices") and _has_rows(con, "v_daily_prices")):
            pytest.skip("no price data yet")
        stray = _scalar(
            con,
            "SELECT count(*) FROM (SELECT DISTINCT stock_id FROM v_adj_daily_prices "
            "EXCEPT SELECT DISTINCT stock_id FROM v_daily_prices)",
        )
        assert stray == 0


class TestDelistedUniverse:
    """The securities TaiwanStockInfo cannot see.

    These all skip cleanly until scripts/discover_delisted.py has run, so the
    suite still passes on a warehouse built before that step existed.
    """

    def test_securities_all_is_one_row_per_id(self, con):
        """A code in both the cumulative master and the delisted list — which
        is what a delisting from now on produces — must not be duplicated."""
        dupes = _scalar(
            con,
            "SELECT count(*) FROM (SELECT stock_id FROM v_securities_all "
            "GROUP BY 1 HAVING count(*) > 1)",
        )
        assert dupes == 0

    def test_securities_all_covers_both_sides(self, con):
        total = _scalar(con, "SELECT count(*) FROM v_securities_all")
        master = _scalar(con, "SELECT count(*) FROM v_stock_info_latest")
        delisted_only = _scalar(
            con,
            "SELECT count(*) FROM v_delisted_securities d WHERE NOT EXISTS "
            "(SELECT 1 FROM v_stock_info_latest s WHERE s.stock_id = d.stock_id)",
        )
        assert total == master + delisted_only

    def test_delisted_flag_matches_the_delisted_list(self, con):
        flagged = _scalar(con, "SELECT count(*) FROM v_securities_all WHERE is_delisted")
        listed = _scalar(con, "SELECT count(*) FROM v_delisted_securities")
        assert flagged == listed

    def test_delisted_shapes_are_in_scope(self, con):
        """Warrants and TDRs must not sneak in through the discovery sweep."""
        if not _has_rows(con, "v_delisted_securities"):
            pytest.skip("no discovery sweep yet")
        stray = _scalar(
            con,
            "SELECT count(*) FROM v_delisted_securities "
            "WHERE shape NOT IN ('common', 'etf')",
        )
        assert stray == 0

    def test_enriched_exposes_the_delisted_flag(self, con):
        cols = {
            r[0] for r in con.execute("DESCRIBE SELECT * FROM v_daily_prices_enriched").fetchall()
        }
        assert {"is_delisted", "delisted_date"} <= cols

    def test_delisted_securities_have_prices(self, con):
        """After the backfill, a recovered security should carry history."""
        if not (_has_rows(con, "v_delisted_securities") and _has_rows(con, "v_daily_prices")):
            pytest.skip("no discovery sweep or no price data yet")
        with_prices = _scalar(
            con,
            "SELECT count(*) FROM v_delisted_securities d "
            "WHERE EXISTS (SELECT 1 FROM v_daily_prices p WHERE p.stock_id = d.stock_id)",
        )
        if with_prices == 0:
            pytest.skip("delisted backfill has not run yet")
        total = _scalar(con, "SELECT count(*) FROM v_delisted_securities")
        assert with_prices > total * 0.5, (
            f"only {with_prices}/{total} delisted securities have prices; "
            "the delisted backfill looks incomplete"
        )

    def test_no_prices_after_delisting_date(self, con):
        """A delisted security must not trade after the date it delisted."""
        if not _has_rows(con, "v_delisted_securities"):
            pytest.skip("no discovery sweep yet")
        stray = _scalar(
            con,
            "SELECT count(*) FROM v_daily_prices p "
            "JOIN v_delisted_securities d USING (stock_id) "
            "WHERE d.delisted_date IS NOT NULL AND p.date > d.delisted_date",
        )
        assert stray == 0


class TestIntegrity:
    def test_no_duplicate_stock_id_date(self, con):
        if not _has_rows(con, "v_daily_prices"):
            pytest.skip("no price data yet")
        dupes = _scalar(
            con,
            "SELECT count(*) FROM (SELECT stock_id, date FROM v_daily_prices "
            "GROUP BY 1, 2 HAVING count(*) > 1)",
        )
        assert dupes == 0

    def test_industry_key_is_unique(self, con):
        dupes = _scalar(
            con,
            "SELECT count(*) FROM (SELECT stock_id, industry, sub_industry "
            "FROM v_industry GROUP BY 1, 2, 3 HAVING count(*) > 1)",
        )
        assert dupes == 0

    def test_calendar_covers_all_price_dates(self, con):
        if not _has_rows(con, "v_daily_prices"):
            pytest.skip("no price data yet")
        uncovered = _scalar(
            con,
            "SELECT count(*) FROM (SELECT DISTINCT date FROM v_daily_prices "
            "EXCEPT SELECT date FROM v_trading_calendar)",
        )
        assert uncovered == 0

    def test_no_future_dated_prices(self, con):
        if not _has_rows(con, "v_daily_prices"):
            pytest.skip("no price data yet")
        latest = _scalar(con, "SELECT max(date) FROM v_daily_prices")
        assert latest <= dt.date.today()

    def test_prices_are_non_negative(self, con):
        if not _has_rows(con, "v_daily_prices"):
            pytest.skip("no price data yet")
        assert _scalar(con, "SELECT count(*) FROM v_daily_prices WHERE close < 0") == 0


class TestGoldenValues:
    """Spot values captured from real FinMind responses."""

    @pytest.mark.parametrize(
        ("view", "expected"),
        [("v_daily_prices", 37.09), ("v_adj_daily_prices", 4.427356)],
    )
    def test_0050_on_2003_07_01(self, con, view, expected):
        if not _has_rows(con, view):
            pytest.skip("no price data yet")
        got = _scalar(
            con, f"SELECT open FROM {view} WHERE stock_id='0050' AND date=DATE '2003-07-01'"
        )
        if got is None:
            pytest.skip("0050 not backfilled yet")
        assert got == pytest.approx(expected)

    def test_adjustment_actually_differs(self, con):
        """If adjusted equals unadjusted, the adjusted feed is not adjusting."""
        if not _has_rows(con, "v_adj_daily_prices"):
            pytest.skip("no adjusted data yet")
        row = con.execute(
            "SELECT close, adj_close FROM v_prices_combined "
            "WHERE stock_id='0050' AND date=DATE '2003-07-01'"
        ).fetchone()
        if not row or row[1] is None:
            pytest.skip("0050 not backfilled yet")
        assert row[0] != row[1]
