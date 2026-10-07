"""Daily incremental update. Entry point for the Windows Task Scheduler job.

Normal cost is a handful of API calls and a few seconds, because one
whole-market call covers every security for a day.

Three things it does beyond "fetch today":

* **Revision window.** FinMind revises same-day figures after the close — the
  2026-08-20 bar for 0050 moved from 39,946,000 to 43,382,378 shares once
  settled, with prices unchanged. So the most recent few trading days are
  re-fetched and overwritten every run, not just newly appeared ones.
* **Gap self-healing.** Any trading day missing from the processed layer is
  re-fetched. A missed run, a laptop that was asleep, a crash mid-write — all
  repair themselves on the next run with no manual intervention.
* **Universe refresh first.** The security master is ingested before the price
  pull, so a security listed today is already in the filter when its first bar
  arrives.

Usage::

    python scripts/daily_update.py
    python scripts/daily_update.py --date 2026-08-24
    python scripts/daily_update.py --revision-window 5
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from finmind_pipeline.database import build as db_build
from finmind_pipeline.ingestion import prices as ingest_prices
from finmind_pipeline.ingestion import tw_industry, tw_stock_info, tw_trading_dates
from finmind_pipeline.processing import dimensions, empty_dates
from finmind_pipeline.processing import prices as process_prices
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.finmind_client import FinMindClient
from finmind_pipeline.utils.logging_setup import setup_logging
from finmind_pipeline.utils.run_manifest import RunManifest
from finmind_pipeline.utils.trading_calendar import load_calendar, trading_days_between

log = logging.getLogger("daily_update")

PRICE_DATASETS = ("daily_prices", "adj_daily_prices")
DEFAULT_REVISION_WINDOW = 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="target date (default: today)")
    ap.add_argument(
        "--revision-window",
        type=int,
        default=DEFAULT_REVISION_WINDOW,
        help="how many recent trading days to re-fetch to pick up upstream revisions",
    )
    ap.add_argument(
        "--max-days",
        type=int,
        default=30,
        help="refuse to run if more days than this need fetching (use backfill.py instead)",
    )
    ap.add_argument("--skip-build", action="store_true", help="do not rebuild DuckDB views")
    args = ap.parse_args(argv)

    setup_logging("ingestion")
    logging.getLogger().setLevel(logging.INFO)
    settings = get_settings()
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    with RunManifest("daily_update") as run, FinMindClient() as client:
        client.log_quota()

        # 1. Refresh dimensions first so the universe filter is current.
        tw_stock_info.ingest(client, today)
        tw_industry.ingest(client, today)
        tw_trading_dates.ingest(client, today)
        # Delisted securities included deliberately. Today's pull will never
        # contain them, but the gap repair below re-fetches whole-market days
        # from anywhere inside the observed range — and filtering one of those
        # by the current master would silently strip every delisted security
        # out of that day, undoing the survivorship fix one day at a time.
        universe = tw_stock_info.load_universe(include_index=True, include_delisted=True)
        run.metric("universe_size", len(universe))

        calendar_path = (
            settings.path(settings.dataset("tw_trading_dates").processed)
            / "expanded_tw_trading_dates.parquet"
        )
        dimensions.process_expanded_trading_dates()
        calendar = load_calendar(calendar_path)

        known_empty = empty_dates.load()
        if known_empty:
            log.info(
                "%d calendar day(s) already confirmed closed; not re-fetching them",
                len(known_empty),
            )

        for dataset_key in PRICE_DATASETS:
            have = process_prices.processed_dates(dataset_key)
            latest_have = max(have) if have else None

            # (a) forward fill: every calendar day after what we already have.
            #     A day with no data yet simply returns empty and is retried
            #     tomorrow, so unpublished days never look like gaps.
            forward_start = latest_have or dt.date.fromisoformat(settings.history_start)
            forward = trading_days_between(calendar, forward_start, today)

            # (b) revision window: re-fetch the tail even though we have it.
            revision = (
                [d for d in trading_days_between(calendar, None, latest_have)][
                    -args.revision_window :
                ]
                if latest_have
                else []
            )

            # (c) gaps strictly inside the observed range, ignoring days already
            #     confirmed closed. A closure is a fact about the exchange, not
            #     about one dataset, so it suppresses the re-fetch for both.
            gaps = [
                d
                for d in trading_days_between(calendar, min(have) if have else None, latest_have)
                if d not in have and d not in known_empty
            ]
            if gaps:
                log.warning("%s: %d gap day(s) to repair: %s", dataset_key, len(gaps), gaps[:10])

            targets = sorted(set(forward) | set(revision) | set(gaps))
            log.info(
                "%s: %d target day(s) (%d forward, %d revision, %d gap)",
                dataset_key,
                len(targets),
                len(forward),
                len(revision),
                len(gaps),
            )

            # An empty or barely-populated warehouse would make `forward` span
            # the whole 8,100-day calendar, i.e. ~16,000 market calls to do
            # badly what one backfill does well in ~6,300 per-ticker calls.
            # Refuse rather than burn the quota.
            if len(targets) > args.max_days:
                raise SystemExit(
                    f"{dataset_key}: {len(targets)} days to fetch exceeds --max-days "
                    f"({args.max_days}). This usually means the warehouse is empty or "
                    f"far behind — run scripts/backfill.py instead. Override with "
                    f"--max-days if you really intend this."
                )

            written = empty = 0
            empty_days: list[dt.date] = []
            for date in targets:
                result = ingest_prices.fetch_market_day(
                    client, dataset_key, date, universe=universe, overwrite=True
                )
                if result.rows_written == 0:
                    empty += 1
                    empty_days.append(date)
                    continue
                process_prices.process_market_day(dataset_key, date)
                written += 1

            # A calendar day that returns nothing and is older than data we
            # already hold is a market closure the published calendar missed,
            # not a gap. Record it so it stops being re-fetched every run.
            if empty_days:
                newest = max(process_prices.processed_dates(dataset_key), default=None)
                known_empty |= empty_dates.record(empty_days, newest_held=newest)

            run.metric(f"{dataset_key}_days_written", written)
            run.metric(f"{dataset_key}_days_empty", empty)
            run.metric(f"{dataset_key}_gaps_repaired", len(gaps))
            run.event(
                "dataset_updated",
                dataset=dataset_key,
                days_written=written,
                days_empty=empty,
                gaps=len(gaps),
            )

        # 2. Rebuild dimensions and the calendar now that new dates exist.
        dimensions.process_all_dimensions()
        run.metric("api_calls", client.call_count)

    if not args.skip_build:
        db_build.build()
        log.info("DuckDB views rebuilt")

    return 0


if __name__ == "__main__":
    sys.exit(main())
