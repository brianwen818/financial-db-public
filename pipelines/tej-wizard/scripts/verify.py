"""Health check for the warehouse. Read-only; safe to run any time.

Prints a pass/fail table and exits non-zero if anything fails, so it can be
chained after a scheduled job or wired into CI.

The golden values deliberately come from **unadjusted** columns. Back-adjusted
prices are rewritten by every dividend and split -- 2330's adjusted close for
2000-09-04 will drift for as long as the company pays a dividend -- so pinning a
test to one would make the suite fail on correct data. ``next_ref_price`` and
``transactions`` for the same bar are settled history and never move.

Usage::

    python scripts/verify.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tej_pipeline.processing import index_schema as IS  # noqa: E402
from tej_pipeline.processing import schema as S  # noqa: E402
from tej_pipeline.settings import get_settings  # noqa: E402

# (label, sql, predicate on the single returned value)
CHECKS: list[tuple[str, str, object]] = [
    (
        "rows present",
        "SELECT count(*) FROM v_adj_daily_prices",
        lambda v: v >= 600_000,
    ),
    (
        "securities present",
        "SELECT count(*) FROM v_securities",
        lambda v: v >= 100,
    ),
    (
        "(stock_id, date) unique",
        "SELECT count(*) - count(DISTINCT (stock_id, date)) FROM v_adj_daily_prices",
        lambda v: v == 0,
    ),
    (
        "no null stock_id",
        "SELECT count(*) FROM v_adj_daily_prices WHERE stock_id IS NULL",
        lambda v: v == 0,
    ),
    (
        "no null date",
        "SELECT count(*) FROM v_adj_daily_prices WHERE date IS NULL",
        lambda v: v == 0,
    ),
    (
        "no null close",
        "SELECT count(*) FROM v_adj_daily_prices WHERE close IS NULL",
        lambda v: v == 0,
    ),
    (
        "history starts 2000-09-04",
        "SELECT min(date)::VARCHAR FROM v_adj_daily_prices",
        lambda v: v == "2000-09-04",
    ),
    (
        "one security per workbook",
        "SELECT count(*) FROM (SELECT source_file FROM v_adj_daily_prices "
        "GROUP BY source_file HAVING count(DISTINCT stock_id) > 1)",
        lambda v: v == 0,
    ),
    (
        "filename matches stock_id",
        "SELECT count(*) FROM (SELECT DISTINCT stock_id, source_file FROM v_adj_daily_prices) "
        "WHERE source_file <> stock_id || '.xlsx'",
        lambda v: v == 0,
    ),
    (
        "every workbook on contract",
        "SELECT count(*) FROM v_workbooks WHERE NOT query_matches_contract",
        lambda v: v == 0,
    ),
    (
        "workbook rows match parsed rows",
        "SELECT count(*) FROM v_workbook_status WHERE rows_delta <> 0",
        lambda v: v == 0,
    ),
    (
        # TIB is 創新板, which arrived with 2258 鴻華先進-創 (listed 2023-11-20)
        # when the universe grew past the original 0050 set. PSB is 興櫃戰略新板
        # (2021-07..2023-12 here), which arrived with the 2026-09-28 company-info
        # expansion: 17 securities passed through it on the way to OTC/TSE.
        # An unknown code here means TEJ started emitting one this pipeline has
        # never seen.
        "market codes known",
        "SELECT count(*) FROM v_adj_daily_prices "
        "WHERE market NOT IN ('TSE','OTC','REG','TIB','PSB')",
        lambda v: v == 0,
    ),
    (
        "golden 2330 2000-09-04 next_ref_price = 133.5",
        "SELECT next_ref_price FROM v_adj_daily_prices "
        "WHERE stock_id = '2330' AND date = DATE '2000-09-04'",
        lambda v: v == 133.5,
    ),
    (
        "golden 2330 2000-09-04 transactions = 3324",
        "SELECT transactions FROM v_adj_daily_prices "
        "WHERE stock_id = '2330' AND date = DATE '2000-09-04'",
        lambda v: v == 3324,
    ),
    (
        "adjusted differs from raw where expected",
        "SELECT count(*) FROM v_adj_daily_prices "
        "WHERE stock_id = '2330' AND date = DATE '2000-09-04' AND close < next_ref_price",
        lambda v: v == 1,
    ),
    (
        "partition keys agree with date",
        "SELECT count(*) FROM v_adj_daily_prices "
        "WHERE year <> year(date) OR month <> month(date)",
        lambda v: v == 0,
    ),
    (
        "view exposes the full processed schema",
        "SELECT count(*) FROM (DESCRIBE v_adj_daily_prices)",
        lambda v: v == len(S.PROCESSED_COLUMNS) + 2,  # + year, month partition keys
    ),
    # --- index constituents (widxs) ---------------------------------------
    (
        "index rows present",
        "SELECT count(*) FROM v_index_constituents",
        lambda v: v >= 800_000,
    ),
    (
        "both indices present",
        "SELECT count(DISTINCT index_id) FROM v_index_constituents",
        lambda v: v == 2,
    ),
    (
        "(index_id, date, stock_id) unique",
        "SELECT count(*) - count(DISTINCT (index_id, date, stock_id)) "
        "FROM v_index_constituents",
        lambda v: v == 0,
    ),
    (
        "no null index stock_id",
        "SELECT count(*) FROM v_index_constituents WHERE stock_id IS NULL",
        lambda v: v == 0,
    ),
    (
        "no null prev_weight_pct",
        "SELECT count(*) FROM v_index_constituents WHERE prev_weight_pct IS NULL",
        lambda v: v == 0,
    ),
    (
        "TWN50 history starts 2002-07-05",
        "SELECT min(date)::VARCHAR FROM v_index_constituents WHERE index_id = 'TWN50'",
        lambda v: v == "2002-07-05",
    ),
    (
        "TM100 history starts 2004-11-30",
        "SELECT min(date)::VARCHAR FROM v_index_constituents WHERE index_id = 'TM100'",
        lambda v: v == "2004-11-30",
    ),
    (
        # Every year workbook owns its dates outright. A date served by two
        # files means one was seeded into the wrong year, which no key check
        # would catch because the rows differ only in provenance.
        "no date served by two index workbooks",
        "SELECT count(*) FROM (SELECT index_id, date FROM v_index_constituents "
        "GROUP BY index_id, date HAVING count(DISTINCT source_file) > 1)",
        lambda v: v == 0,
    ),
    (
        "index workbook filename matches its year",
        "SELECT count(*) FROM (SELECT DISTINCT source_file, year FROM v_index_constituents) "
        "WHERE source_file NOT LIKE '%/' || year || '.xlsx'",
        lambda v: v == 0,
    ),
    (
        "every index workbook on contract",
        "SELECT count(*) FROM v_index_workbooks WHERE NOT query_matches_contract",
        lambda v: v == 0,
    ),
    (
        "index workbook rows match parsed rows",
        "SELECT count(*) FROM v_index_workbook_status WHERE rows_delta <> 0",
        lambda v: v == 0,
    ),
    (
        # Not a fixed 50/100: measured 46-51 and 98-102. The band only has to
        # exclude a half-landed refresh, which shows up as a fraction of a set.
        "constituent counts inside the measured band",
        "SELECT count(*) FROM v_index_daily_coverage WHERE "
        "(index_id = 'TWN50' AND constituents NOT BETWEEN 44 AND 53) OR "
        "(index_id = 'TM100' AND constituents NOT BETWEEN 95 AND 104)",
        lambda v: v == 0,
    ),
    (
        # TEJ's own weights must close. The exception is 2002, when TWN50 was
        # still filling up to 50 names and the published weights genuinely sum
        # to as little as 93.69.
        "index weights sum to 100 after 2002",
        "SELECT count(*) FROM v_index_daily_coverage "
        "WHERE date >= DATE '2003-01-01' AND total_weight_pct NOT BETWEEN 99.5 AND 100.5",
        lambda v: v == 0,
    ),
    (
        "golden TWN50 2015-06-30 holds 2330 at 23.8845%",
        "SELECT prev_weight_pct FROM v_index_constituents "
        "WHERE index_id = 'TWN50' AND date = DATE '2015-06-30' AND stock_id = '2330'",
        lambda v: v == 23.8845,
    ),
    (
        "index partition keys agree with date",
        "SELECT count(*) FROM v_index_constituents "
        "WHERE year <> year(date) OR month <> month(date)",
        lambda v: v == 0,
    ),
    (
        "index view exposes the full processed schema",
        "SELECT count(*) FROM (DESCRIBE v_index_constituents)",
        lambda v: v == len(IS.PROCESSED_COLUMNS) + 2,  # + year, month partition keys
    ),
    (
        # The constituent list is published for the NEXT session, so it is
        # normally one session ahead of the price layer and never behind it.
        "constituent list is not behind the price layer",
        "SELECT datediff('day', (SELECT max(date) FROM v_adj_daily_prices), "
        "(SELECT min(date) FROM v_index_last_trading_day))",
        lambda v: v >= 0,
    ),
    (
        # Every index member must be priceable, or a point-in-time
        # cross-section silently drops it. This going non-zero is the normal,
        # expected signal that a security has newly joined TWN50 or TM100 and
        # needs `scripts/create_workbooks.py --tickers <id>` run once.
        "every index member has price history",
        "SELECT count(*) FROM v_index_universe WHERE NOT has_price_history",
        lambda v: v == 0,
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="print only failures")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.duckdb_path.exists():
        print(f"ERROR: {settings.duckdb_path} does not exist; run scripts/build_db.py")
        return 2

    con = duckdb.connect(str(settings.duckdb_path), read_only=True)
    width = max(len(label) for label, _, _ in CHECKS)
    failures = 0
    try:
        for label, sql, predicate in CHECKS:
            try:
                row = con.execute(sql).fetchone()
                value = row[0] if row else None
                ok = bool(predicate(value))
            except Exception as exc:  # noqa: BLE001
                value, ok = f"ERROR: {str(exc)[:80]}", False
            if not ok:
                failures += 1
            if not ok or not args.quiet:
                mark = "PASS" if ok else "FAIL"
                print(f"  [{mark}] {label:<{width}}  {value}")
    finally:
        con.close()

    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
