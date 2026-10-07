"""Weekly maintenance: adjusted-price rebuild plus reconciliation.

Two jobs in one run, both weekly because that is the cadence the adjusted
series forces.

**Adjusted-price full refresh.** Every dividend or split rewrites the whole
back-adjusted history for that ticker, so appending a day leaves every earlier
row stale. There is no way to patch it incrementally without knowing the
corporate-action calendar, so all ~3,100 tickers are re-fetched and the
processed partitions rebuilt from scratch. The security master is refreshed
*first*, so newly listed securities are included.

**Unadjusted reconciliation.** The unadjusted series is genuinely append-only,
which makes verification a set-comparison over dates rather than a per-ticker
audit:

    A  missing days   — calendar days absent from the processed layer
    B  thin days      — days whose row count collapses against their neighbours,
                        which means a partial write rather than a quiet session
    C  absent tickers — currently listed securities with no rows at all
    D  stale tickers  — currently listed securities whose last bar is far behind

A and B cost nothing when clean. C and D are the long-tail safety net.

C and D are scoped to the *current* security master, not to everything the
warehouse holds. Delisted securities are deliberately outside their reach:
their history cannot change, their last bar is permanently old, and checking
them would mean re-fetching hundreds of frozen tickers every week forever. A
and B stay scoped to the full universe, because a repaired historical day must
keep the delisted securities that traded on it.

Usage::

    python scripts/weekly_refresh.py
    python scripts/weekly_refresh.py --dry-run
    python scripts/weekly_refresh.py --skip-adj      # reconciliation only
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

import duckdb

from finmind_pipeline.database import build as db_build
from finmind_pipeline.ingestion import prices as ingest_prices
from finmind_pipeline.ingestion import (
    tw_delisting,
    tw_industry,
    tw_stock_info,
    tw_trading_dates,
)
from finmind_pipeline.processing import dimensions, empty_dates
from finmind_pipeline.processing import prices as process_prices
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.finmind_client import FinMindClient
from finmind_pipeline.utils.logging_setup import setup_logging
from finmind_pipeline.utils.run_manifest import RunManifest
from finmind_pipeline.utils.trading_calendar import (
    load_calendar,
    shift_trading_days,
    trading_days_between,
)

log = logging.getLogger("weekly_refresh")

STALE_TRADING_DAYS = 20


def reconcile_daily_prices(
    client: FinMindClient,
    universe: set[str],
    calendar: list[dt.date],
    dry_run: bool = False,
    current: set[str] | None = None,
) -> dict[str, list]:
    """Run checks A-D and repair what they find.

    Two universes, and conflating them breaks something either way:

    ``universe``
        Everything that ever traded, delisted included. Used to filter the
        whole-market re-pull that repairs a day — a repaired 2010 day must keep
        its delisted securities, or the repair reintroduces survivorship bias
        into that day.

    ``current``
        What is listed today. Used for the absent/stale checks, which ask
        "should this ticker have fresh data?". A delisted security's answer is
        no, permanently, and asking it of the full universe would re-fetch
        every delisted ticker every week forever.
    """
    current = universe if current is None else current
    settings = get_settings()
    con = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        present = {r[0] for r in con.execute("SELECT DISTINCT date FROM v_daily_prices").fetchall()}
        thin = [r[0] for r in con.execute("SELECT date FROM v_thin_trading_days").fetchall()]
        have_ids = {
            r[0] for r in con.execute("SELECT DISTINCT stock_id FROM v_daily_prices").fetchall()
        }
        last_seen = dict(
            con.execute(
                "SELECT stock_id, max(date) FROM v_daily_prices GROUP BY stock_id"
            ).fetchall()
        )
    finally:
        con.close()

    if not present:
        raise RuntimeError("no processed daily prices; run backfill first")

    lo, hi = min(present), max(present)

    # A. days the calendar has but we do not, strictly inside the observed
    #    range. Days already confirmed closed are excluded: the exchange did
    #    not trade, so there is nothing to repair and re-pulling them would
    #    cost a call a week for ever. This matches v_missing_trading_days.
    known_empty = empty_dates.load()
    missing = [
        d
        for d in trading_days_between(calendar, lo, hi)
        if d not in present and d not in known_empty
    ]

    # TaiwanStockInfo keeps listing securities long after they delist — 250 of
    # them, measured — so "in the current master" is not the same as "should
    # have fresh data". The delisting log overrules the master. A code whose
    # last bar is *after* its recorded delisting date has relisted, so it stays
    # checkable.
    delisted_on = tw_delisting.load_delisted_dates()
    checkable = {
        s
        for s in current
        if s not in delisted_on
        or (last_seen.get(s) is not None and last_seen[s] > delisted_on[s])
    }
    suppressed = len(current) - len(checkable)
    if suppressed:
        log.info("%d listed code(s) suppressed: upstream records them as delisted", suppressed)

    # C. currently listed securities with no price rows at all
    absent = sorted(checkable - have_ids)

    # D. currently listed securities whose most recent bar is well behind the
    #    market. Scoped to `checkable` so neither a delisting nor a stale master
    #    entry becomes a permanent weekly re-fetch of history that cannot change.
    cutoff = shift_trading_days(calendar, hi, -STALE_TRADING_DAYS)
    stale = sorted(s for s in checkable & have_ids if last_seen.get(s) and last_seen[s] < cutoff)

    log.info(
        "reconciliation: %d missing day(s), %d thin day(s), %d absent ticker(s), %d stale ticker(s)",
        len(missing),
        len(thin),
        len(absent),
        len(stale),
    )
    for label, items in (("missing", missing), ("thin", thin)):
        if items:
            log.warning("  %s days: %s%s", label, items[:10], " ..." if len(items) > 10 else "")

    if dry_run:
        return {"missing": missing, "thin": thin, "absent": absent, "stale": stale, "repaired": []}

    repaired: list = []
    still_empty: list[dt.date] = []
    # A + B are repaired the same way: re-pull the whole market for that date,
    # which is complete and authoritative.
    for date in sorted(set(missing) | set(thin)):
        result = ingest_prices.fetch_market_day(
            client, "daily_prices", date, universe=universe, overwrite=True
        )
        if result.rows_written:
            process_prices.process_market_day("daily_prices", date)
            repaired.append(date)
        else:
            still_empty.append(date)

    # A day that re-fetches to nothing, and sits behind data we already hold,
    # is a closure the published calendar got wrong. Remember it rather than
    # reporting the same false gap every week.
    if still_empty:
        empty_dates.record(still_empty, newest_held=hi)

    # C + D need a per-ticker history fetch.
    for stock_id in absent + stale:
        ingest_prices.fetch_ticker(client, "daily_prices", stock_id)

    return {
        "missing": missing,
        "thin": thin,
        "absent": absent,
        "stale": stale,
        "repaired": repaired,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report findings, change nothing")
    ap.add_argument("--skip-adj", action="store_true", help="skip the adjusted-price refresh")
    ap.add_argument("--skip-reconcile", action="store_true", help="skip reconciliation")
    args = ap.parse_args(argv)

    setup_logging("ingestion")
    logging.getLogger().setLevel(logging.INFO)
    settings = get_settings()

    with RunManifest("weekly_refresh") as run, FinMindClient() as client:
        client.log_quota()

        # 1. Security master first — it defines the universe for everything below.
        if not args.dry_run:
            tw_stock_info.ingest(client)
            tw_industry.ingest(client)
            tw_trading_dates.ingest(client)
            # One call. Keeps names and dates arriving for securities that
            # delist from here on, so they do not land in the warehouse as
            # a bare code the way the historical ones did.
            tw_delisting.ingest(client)
            dimensions.process_expanded_trading_dates()

        # Wide for repairs, narrow for "should this be fresh?" — see
        # reconcile_daily_prices for why both are needed.
        universe = tw_stock_info.load_universe(include_index=True, include_delisted=True)
        current = tw_stock_info.load_current_universe(include_index=True)
        run.metric("universe_size", len(universe))
        run.metric("universe_current", len(current))

        calendar = load_calendar(
            settings.path(settings.dataset("tw_trading_dates").processed)
            / "expanded_tw_trading_dates.parquet"
        )

        # 2. Adjusted prices: full re-fetch, then a staged rebuild so the live
        #    dataset stays complete and queryable throughout.
        if not args.skip_adj and not args.dry_run:
            # Currently listed only: a delisted security has no further
            # corporate actions, so its adjusted history is frozen. FinMind
            # does not serve adjusted prices for delisted codes at all — 7 of
            # 767 have any — so refreshing them would be ~760 empty calls.
            log.info("adjusted-price full refresh: %d tickers", len(current))
            written = ingest_prices.fetch_tickers(
                client, "adj_daily_prices", sorted(current), overwrite=True
            )
            ok = sum(1 for v in written.values() if v > 0)
            run.metric("adj_tickers_fetched", len(written))
            run.metric("adj_tickers_with_data", ok)
            run.metric("adj_rows", sum(v for v in written.values() if v > 0))
            run.metric("adj_failed", sum(1 for v in written.values() if v == -1))

            stats = process_prices.rebuild_from_raw("adj_daily_prices", staged=True)
            run.metric("adj_processed_rows", stats.rows_written)
            run.metric("adj_processed_days", stats.dates_written)
        elif args.skip_adj:
            log.info("skipping adjusted-price refresh (--skip-adj)")

        # 3. Reconcile the unadjusted series.
        if not args.skip_reconcile:
            findings = reconcile_daily_prices(
                client, universe, calendar, args.dry_run, current=current
            )
            for key in ("missing", "thin", "absent", "stale", "repaired"):
                run.metric(f"reconcile_{key}", len(findings[key]))
            run.event(
                "reconciliation",
                missing_days=[str(d) for d in findings["missing"][:50]],
                thin_days=[str(d) for d in findings["thin"][:50]],
                absent_tickers=findings["absent"][:50],
                stale_tickers=findings["stale"][:50],
            )
            if not args.dry_run and (findings["absent"] or findings["stale"]):
                process_prices.rebuild_from_raw("daily_prices", staged=True)

        # 4. Rebuild dimensions with any newly arrived dates.
        if not args.dry_run:
            dimensions.process_all_dimensions()
        run.metric("api_calls", client.call_count)

    if not args.dry_run:
        db_build.build()
        log.info("DuckDB views rebuilt")

    return 0


if __name__ == "__main__":
    sys.exit(main())
