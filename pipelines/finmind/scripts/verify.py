"""Health check for the warehouse. Read-only; safe to run any time.

Prints a pass/fail table and exits non-zero if anything fails, so it can be
chained after a scheduled job or wired into CI.

Usage::

    python scripts/verify.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import duckdb

from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.parquet_io import dataset_file_count, dataset_size_bytes

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


class Checks:
    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        self.results: list[tuple[str, str, str]] = []

    def scalar(self, sql: str):
        row = self.con.execute(sql).fetchone()
        return row[0] if row else None

    def record(self, name: str, status: str, detail: str) -> None:
        self.results.append((name, status, detail))

    def expect(self, name: str, actual, expected, detail: str = "") -> None:
        ok = actual == expected
        self.record(
            name,
            PASS if ok else FAIL,
            detail or f"{actual!r}" + ("" if ok else f" (expected {expected!r})"),
        )

    def expect_zero(self, name: str, sql: str, detail: str = "") -> None:
        n = self.scalar(sql)
        self.record(name, PASS if n == 0 else FAIL, detail or f"{n:,} offending row(s)")

    def run(self) -> None:
        s = get_settings()

        # --- scale ---
        for view in ("v_daily_prices", "v_adj_daily_prices"):
            n = self.scalar(f"SELECT count(*) FROM {view}")
            ids = self.scalar(f"SELECT count(DISTINCT stock_id) FROM {view}")
            lo, hi = self.con.execute(f"SELECT min(date), max(date) FROM {view}").fetchone()
            days = self.scalar(f"SELECT count(DISTINCT date) FROM {view}")
            self.record(
                f"{view} populated",
                PASS if n > 1_000_000 else WARN,
                f"{n:,} rows · {ids:,} securities · {days:,} days · {lo} .. {hi}",
            )

        # --- dimensions ---
        self.expect(
            "stock_info NULL snapshot_date == 32 (the literal 'None' rows)",
            self.scalar("SELECT count(*) FROM v_stock_info_history WHERE snapshot_date IS NULL"),
            32,
        )
        self.expect(
            "index pseudo-tickers == 32",
            self.scalar("SELECT count(*) FROM v_indices"),
            32,
        )
        n_cal = self.scalar("SELECT count(*) FROM v_trading_calendar")
        self.record(
            "trading calendar >= 8,141 days",
            PASS if n_cal >= 8141 else FAIL,
            f"{n_cal:,} days",
        )
        derived = self.scalar("SELECT count(*) FROM v_trading_calendar WHERE source = 'derived'")
        sat = self.scalar(
            "SELECT count(*) FROM v_trading_calendar "
            "WHERE source = 'derived' AND dayofweek(date) = 6"
        )
        self.record(
            "pre-1999 calendar derived from prices",
            PASS if derived >= 1204 else WARN,
            f"{derived:,} derived days, {sat} of them Saturdays (the exchange traded Saturdays until 1998)",
        )

        # --- integrity ---
        self.expect_zero(
            "no duplicate (stock_id, date) in daily prices",
            "SELECT count(*) FROM (SELECT stock_id, date FROM v_daily_prices "
            "GROUP BY 1,2 HAVING count(*) > 1)",
        )
        self.expect_zero(
            "no duplicate (stock_id, date) in adjusted prices",
            "SELECT count(*) FROM (SELECT stock_id, date FROM v_adj_daily_prices "
            "GROUP BY 1,2 HAVING count(*) > 1)",
        )
        self.expect_zero(
            "industry key (stock_id, industry, sub_industry) unique",
            "SELECT count(*) FROM (SELECT stock_id, industry, sub_industry FROM v_industry "
            "GROUP BY 1,2,3 HAVING count(*) > 1)",
        )
        # The master is a history of content changes, not of crawls. Upstream
        # re-stamps most rows with the crawl date on every pull, so a dedupe
        # that keeps the stamp would re-record the same fact daily -- ~3,300
        # rows a run -- and bury the real reclassifications this table exists
        # to hold. Same failure mode as the industry check above.
        self.expect_zero(
            "stock_info has no content duplicates",
            "SELECT count(*) FROM (SELECT stock_id, stock_name, industry_category, "
            "type, is_index FROM v_stock_info_history GROUP BY 1,2,3,4,5 HAVING count(*) > 1)",
        )
        # A back-adjusted value belongs to the basis it was fetched under, and a
        # market-day row is a snapshot of that one day's basis. The weekly
        # per-ticker refresh replaces them, so only the days since it should be
        # market-sourced. A longer tail is what left 6669 with a false -67%
        # break around its 2026-09-02 ex-right date: 19 such days had built up.
        market_days = self.scalar(
            "SELECT count(DISTINCT date) FROM v_adj_daily_prices WHERE source = 'market'"
        )
        self.record(
            "adjusted prices: market-day snapshots limited to the latest week",
            PASS if market_days <= 7 else WARN,
            f"{market_days} day(s) sourced from market-day pulls "
            f"(mixed adjustment bases until the weekly per-ticker refresh replaces them)",
        )
        self.expect_zero(
            "every price date is in the calendar",
            "SELECT count(*) FROM (SELECT DISTINCT date FROM v_daily_prices "
            "EXCEPT SELECT date FROM v_trading_calendar)",
        )
        self.expect_zero(
            "no future-dated prices",
            f"SELECT count(*) FROM v_daily_prices WHERE date > DATE '{dt.date.today()}'",
        )

        # --- scope ---
        # Checked against v_securities_all, not v_stock_info_latest: the master
        # is current-state, so a delisted security is legitimately in the prices
        # and legitimately absent from it.
        self.expect_zero(
            "no out-of-universe securities (warrants/TDRs excluded)",
            "SELECT count(DISTINCT stock_id) FROM v_daily_prices p WHERE NOT EXISTS "
            "(SELECT 1 FROM v_securities_all s WHERE s.stock_id = p.stock_id)",
        )
        self.expect_zero(
            "adjusted securities are a subset of unadjusted",
            "SELECT count(*) FROM (SELECT DISTINCT stock_id FROM v_adj_daily_prices "
            "EXCEPT SELECT DISTINCT stock_id FROM v_daily_prices)",
        )

        # --- survivorship ---
        # Reported rather than asserted: 301 of the recovered codes delisted
        # before 2004-02-11 and have no upstream data at all, and the adjusted
        # feed does not carry delisted securities in the first place. Both are
        # upstream facts, so a threshold here would only ever be noise.
        delisted = self.scalar("SELECT count(*) FROM v_delisted_securities")
        with_px = self.scalar(
            "SELECT count(*) FROM v_delisted_securities d WHERE EXISTS "
            "(SELECT 1 FROM v_daily_prices p WHERE p.stock_id = d.stock_id)"
        )
        with_adj = self.scalar(
            "SELECT count(*) FROM v_delisted_securities d WHERE EXISTS "
            "(SELECT 1 FROM v_adj_daily_prices a WHERE a.stock_id = d.stock_id)"
        )
        self.record(
            "delisted securities recovered",
            PASS if delisted else WARN,
            f"{delisted:,} known · {with_px:,} with unadjusted prices · "
            f"{with_adj:,} with adjusted (upstream gap; adjusted stays survivor-only)"
            if delisted
            else "none; run scripts/discover_delisted.py",
        )

        # --- coverage ---
        missing = self.scalar("SELECT count(*) FROM v_missing_trading_days")
        self.record(
            "no missing trading days",
            PASS if missing == 0 else WARN,
            f"{missing:,} missing (weekly_refresh repairs these)",
        )
        known_empty = self.scalar("SELECT count(*) FROM v_known_empty_days")
        self.record(
            "calendar days confirmed closed",
            PASS,
            f"{known_empty:,} day(s) the published calendar lists but nothing traded "
            f"(excluded from the gap report)",
        )
        thin = self.scalar("SELECT count(*) FROM v_thin_trading_days")
        self.record(
            "no suspiciously thin days",
            PASS if thin == 0 else WARN,
            f"{thin:,} thin (row count < 50% of neighbours)",
        )

        # --- golden values from real FinMind responses ---
        for view, expected in (("v_daily_prices", 37.09), ("v_adj_daily_prices", 4.427356)):
            got = self.scalar(
                f"SELECT open FROM {view} WHERE stock_id = '0050' AND date = DATE '2003-07-01'"
            )
            self.record(
                f"golden: 0050 2003-07-01 open in {view}",
                PASS if got == expected else FAIL,
                f"{got} (expected {expected})",
            )

        # --- adjustment is actually applied ---
        row = self.con.execute(
            "SELECT close, adj_close FROM v_prices_combined "
            "WHERE stock_id = '0050' AND date = DATE '2003-07-01'"
        ).fetchone()
        self.record(
            "adjusted series differs from unadjusted",
            PASS if row and row[1] is not None and row[0] != row[1] else FAIL,
            f"close={row[0] if row else None} adj_close={row[1] if row else None}",
        )

        # --- storage ---
        for label, key in (
            ("daily-prices", "daily_prices"),
            ("adj-daily-prices", "adj_daily_prices"),
        ):
            root = s.path(s.dataset(key).processed)
            self.record(
                f"storage: {label}",
                PASS,
                f"{dataset_file_count(root):,} files · {dataset_size_bytes(root) / 1e6:,.0f} MB",
            )

        # --- macros ---
        # build.verify() enumerates information_schema.tables, where table macros
        # do not appear -- so the query entry point the docs actually recommend
        # is the one object nothing else smoke-tests. Each macro is exercised over
        # a range that must be non-empty, which also proves partition pruning
        # still resolves `year`.
        for macro in ("daily_prices_between", "adj_daily_prices_between",
                      "prices_combined_between"):
            try:
                n = self.scalar(f"SELECT count(*) FROM {macro}('2024-01-01', '2024-03-31')")
                self.record(f"macro {macro}()", PASS if n else FAIL, f"{n:,} rows in 2024Q1")
            except duckdb.Error as exc:
                self.record(f"macro {macro}()", FAIL, str(exc)[:120])

        # --- pipeline runs ---
        failed = self.scalar("SELECT count(*) FROM v_ingestion_runs WHERE status = 'failed'")
        self.record(
            "no failed pipeline runs on record",
            PASS if failed == 0 else WARN,
            f"{failed} failed run(s)",
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)

    settings = get_settings()
    if not settings.duckdb_path.exists():
        print(f"database not found: {settings.duckdb_path}\nRun scripts/build_db.py first.")
        return 1

    con = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        checks = Checks(con)
        checks.run()
    finally:
        con.close()

    width = max(len(n) for n, _, _ in checks.results)
    print(f"\n{'check'.ljust(width)}  status  detail")
    print("-" * (width + 10 + 60))
    for name, status, detail in checks.results:
        print(f"{name.ljust(width)}  {status:<6}  {detail}")

    failures = sum(1 for _, s, _ in checks.results if s == FAIL)
    warnings = sum(1 for _, s, _ in checks.results if s == WARN)
    print(
        f"\n{len(checks.results)} checks · "
        f"{len(checks.results) - failures - warnings} passed · "
        f"{warnings} warnings · {failures} failures"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
