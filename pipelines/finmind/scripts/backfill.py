"""One-off historical backfill: fetch every ticker's full price history.

Roughly 3,585 tickers x 2 datasets = ~7,200 API calls, about 80 minutes at the
Sponsor budget of 5,400/hr. The run is checkpointed after every ticker, so
``--resume`` picks up exactly where an interrupted run stopped.

Fetching per ticker (rather than per day) is what makes this affordable: one
call returns a ticker's entire history with no row cap, whereas the whole-market
endpoint returns a single day per call and would need ~8,100 calls per dataset.

The universe is the security master *plus* the delisted securities recovered by
``scripts/discover_delisted.py``. Without those, the warehouse holds survivors
only — see ``ingestion/universe_discovery.py`` for why that matters.

Usage::

    python scripts/backfill.py --dry-run
    python scripts/backfill.py --tickers 0050,2330
    python scripts/backfill.py --universe delisted     # only the recovered ones
    python scripts/backfill.py --resume
    python scripts/backfill.py --datasets daily_prices
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from finmind_pipeline.ingestion import prices as ingest_prices
from finmind_pipeline.ingestion import tw_industry, tw_stock_info, tw_trading_dates
from finmind_pipeline.processing import dimensions
from finmind_pipeline.processing import prices as process_prices
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.finmind_client import FinMindClient, FinMindQuotaError
from finmind_pipeline.utils.fsutil import replace_with_retry
from finmind_pipeline.utils.logging_setup import setup_logging
from finmind_pipeline.utils.run_manifest import RunManifest

log = logging.getLogger("backfill")

PRICE_DATASETS = ("daily_prices", "adj_daily_prices")


def load_checkpoint(path: Path) -> dict[str, dict[str, int]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_checkpoint(path: Path, state: dict[str, dict[str, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    replace_with_retry(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tickers", help="comma-separated subset, for validation runs")
    ap.add_argument(
        "--universe",
        choices=("all", "listed", "delisted"),
        default="all",
        help=(
            "which securities to fetch: 'listed' is the security master, "
            "'delisted' is only what the discovery sweep recovered, "
            "'all' is both (default)"
        ),
    )
    ap.add_argument(
        "--datasets",
        default=",".join(PRICE_DATASETS),
        help=f"comma-separated subset of {PRICE_DATASETS}",
    )
    ap.add_argument("--resume", action="store_true", help="skip tickers already done")
    ap.add_argument("--dry-run", action="store_true", help="plan only, no API calls")
    ap.add_argument(
        "--skip-dimensions",
        action="store_true",
        help="do not refresh stock_info/industry/calendar first",
    )
    ap.add_argument(
        "--no-process",
        action="store_true",
        help="land raw only; skip the raw->processed rebuild",
    )
    args = ap.parse_args(argv)

    setup_logging("ingestion")
    settings = get_settings()
    datasets = [d.strip() for d in args.datasets.split(",") if d.strip()]
    for d in datasets:
        if d not in PRICE_DATASETS:
            ap.error(f"unknown dataset {d!r}; choose from {PRICE_DATASETS}")

    checkpoint_path = settings.backfill_checkpoint
    state = load_checkpoint(checkpoint_path) if args.resume else {}

    with RunManifest("backfill") as run, FinMindClient() as client:
        client.log_quota()

        # The security master defines the universe, so it must be current
        # before we decide what to fetch — otherwise newly listed securities
        # are silently skipped for the whole backfill.
        if not args.skip_dimensions and not args.dry_run:
            tw_stock_info.ingest(client)
            tw_industry.ingest(client)
            tw_trading_dates.ingest(client)

        if args.tickers:
            universe = [t.strip() for t in args.tickers.split(",") if t.strip()]
        elif args.universe == "delisted":
            # Recovering only the securities the master cannot see. Empty until
            # scripts/discover_delisted.py has run.
            universe = sorted(tw_stock_info.load_delisted_universe())
            if not universe:
                ap.error(
                    "no delisted securities known; run scripts/discover_delisted.py first"
                )
        else:
            universe = sorted(
                tw_stock_info.load_universe(
                    include_index=True,
                    include_delisted=args.universe == "all",
                )
            )

        total = len(universe) * len(datasets)
        log.info(
            "backfill plan: %d tickers x %d datasets = %d calls (~%.0f min at %d/hr)",
            len(universe),
            len(datasets),
            total,
            total / settings.rate_budget_per_hour * 60,
            settings.rate_budget_per_hour,
        )
        run.metric("planned_calls", total)
        run.metric("universe_size", len(universe))

        if args.dry_run:
            log.info("dry run: first 10 tickers %s", universe[:10])
            return 0

        for dataset_key in datasets:
            # Per-dataset, so the rate and ETA reflect this dataset only —
            # a shared start time makes the second dataset look 10x slower.
            started = time.monotonic()
            done = state.setdefault(dataset_key, {})
            # The checkpoint is a hint; the raw directory is the truth. A
            # ticker recorded as fetched whose file is gone — raw deleted, a
            # checkpoint carried over from a different run — must be fetched
            # again, or --resume would skip it for ever and quietly leave a
            # hole. Entries recorded as 0 wrote no file by design (the API
            # returned nothing), so they are not evidence of anything missing.
            if args.resume:
                lost = [
                    t
                    for t, rows in done.items()
                    if rows > 0 and not ingest_prices.ticker_path(dataset_key, t).exists()
                ]
                for t in lost:
                    del done[t]
                if lost:
                    log.warning(
                        "%s: %d checkpointed ticker(s) have no raw file; re-fetching them",
                        dataset_key,
                        len(lost),
                    )

            # A failure is recorded as -1, so "already attempted" is not the same
            # as "done". Resuming must retry failures, or a ticker lost to a
            # transient error would never be fetched again.
            pending = [t for t in universe if t not in done or done[t] == -1]
            retries = sum(1 for t in pending if t in done)
            log.info(
                "%s: %d to fetch (%d already succeeded, %d being retried after failure)",
                dataset_key,
                len(pending),
                sum(1 for v in done.values() if v >= 0),
                retries,
            )

            for i, stock_id in enumerate(pending, start=1):
                try:
                    result = ingest_prices.fetch_ticker(client, dataset_key, stock_id)
                    done[stock_id] = result.rows_written
                except FinMindQuotaError:
                    log.error("quota exhausted; checkpoint saved, rerun with --resume")
                    save_checkpoint(checkpoint_path, state)
                    raise
                except Exception as exc:  # noqa: BLE001 - never lose 3,000 tickers to one
                    log.warning("%s %s failed: %s", dataset_key, stock_id, exc)
                    done[stock_id] = -1

                if i % 50 == 0 or i == len(pending):
                    save_checkpoint(checkpoint_path, state)
                    elapsed = time.monotonic() - started
                    rate = i / elapsed * 60 if elapsed else 0
                    remaining = (len(pending) - i) / rate if rate else 0
                    with_data = sum(1 for v in done.values() if v > 0)
                    log.info(
                        "%s %d/%d (%.0f/min, ~%.0f min left) | %d with data, %s rows",
                        dataset_key,
                        i,
                        len(pending),
                        rate,
                        remaining,
                        with_data,
                        f"{sum(v for v in done.values() if v > 0):,}",
                    )

            save_checkpoint(checkpoint_path, state)
            empty = sum(1 for v in done.values() if v == 0)
            failed = sum(1 for v in done.values() if v == -1)
            run.metric(f"{dataset_key}_tickers", len(done))
            run.metric(f"{dataset_key}_rows", sum(v for v in done.values() if v > 0))
            run.metric(f"{dataset_key}_empty", empty)
            run.metric(f"{dataset_key}_failed", failed)
            log.info(
                "%s done: %d tickers, %s rows, %d empty, %d failed",
                dataset_key,
                len(done),
                f"{sum(v for v in done.values() if v > 0):,}",
                empty,
                failed,
            )

        run.metric("api_calls", client.call_count)

        if not args.no_process:
            log.info("rebuilding processed layer from raw")
            for dataset_key in datasets:
                stats = process_prices.rebuild_from_raw(dataset_key)
                run.metric(f"{dataset_key}_processed_rows", stats.rows_written)
                run.metric(f"{dataset_key}_processed_days", stats.dates_written)
            dimensions.process_all_dimensions()

    return 0


if __name__ == "__main__":
    sys.exit(main())
