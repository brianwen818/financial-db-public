"""Recover the securities the security master cannot see.

``TaiwanStockInfo`` is current-state, so anything that delisted before this
project's first snapshot (2026-08-26) was never recorded and never backfilled.
This sweeps one whole-market day per month from 2004-02-11 and records every
code that traded, then combines that with ``TaiwanStockDelisting`` to produce
``processed/delisted-securities/``.

Read-only with respect to prices: it lands a discovery snapshot and the
delisting log, and touches no price data. Run ``scripts/backfill.py
--universe delisted`` afterwards to actually fetch their history.

Cost: 271 whole-market calls plus one, about 4-5 minutes at the Sponsor budget.

Usage::

    python scripts/discover_delisted.py --dry-run
    python scripts/discover_delisted.py
    python scripts/discover_delisted.py --start 2004-02-11 --end 2026-08-26
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from finmind_pipeline.ingestion import tw_delisting, tw_stock_info, universe_discovery
from finmind_pipeline.processing import dimensions
from finmind_pipeline.settings import get_settings
from finmind_pipeline.utils.finmind_client import FinMindClient
from finmind_pipeline.utils.logging_setup import setup_logging
from finmind_pipeline.utils.run_manifest import RunManifest
from finmind_pipeline.utils.trading_calendar import load_calendar

log = logging.getLogger("discover_delisted")


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", help="first date to sample (default: 2004-02-11)")
    ap.add_argument("--end", help="last date to sample (default: latest trading day)")
    ap.add_argument("--dry-run", action="store_true", help="plan only, no API calls")
    ap.add_argument(
        "--skip-delisting",
        action="store_true",
        help="do not refresh TaiwanStockDelisting first",
    )
    args = ap.parse_args(argv)

    setup_logging("ingestion")
    logging.getLogger().setLevel(logging.INFO)
    settings = get_settings()

    calendar = load_calendar(
        settings.path(settings.dataset("tw_trading_dates").processed)
        / "expanded_tw_trading_dates.parquet"
    )
    # The calendar runs to the end of the publishing year; sampling days that
    # have not happened yet would just burn calls on empty responses.
    end = _date(args.end) or min(
        max(calendar), dimensions.latest_trading_day() or max(calendar)
    )
    days = universe_discovery.monthly_sample_days(calendar, _date(args.start), end)

    log.info(
        "discovery plan: %d months to sample (%s .. %s), ~%.0f min at %d/hr",
        len(days),
        days[0] if days else None,
        days[-1] if days else None,
        (len(days) + 1) / settings.rate_budget_per_hour * 60,
        settings.rate_budget_per_hour,
    )
    if args.dry_run:
        log.info("dry run: first 12 sample days %s", [d.isoformat() for d in days[:12]])
        return 0

    def progress(i: int, total: int, day: dt.date, seen: int) -> None:
        if i % 20 == 0 or i == total:
            log.info("  [%d/%d] %s — %d codes seen so far", i, total, day, seen)

    with RunManifest("discover_delisted") as run, FinMindClient() as client:
        client.log_quota()

        if not args.skip_delisting:
            tw_delisting.ingest(client)

        _, result = universe_discovery.ingest(
            client,
            calendar,
            start=_date(args.start),
            end=end,
            on_progress=progress,
        )
        run.metric("days_sampled", len(result.days_sampled))
        run.metric("days_empty", len(result.days_empty))
        run.metric("codes_seen", len(result.observations))
        run.metric("api_calls", client.call_count)

        dimensions.process_delisting()
        dimensions.process_delisted_securities()

        listed = tw_stock_info.load_universe(include_index=True)
        delisted = tw_stock_info.load_delisted_universe()
        run.metric("universe_listed", len(listed))
        run.metric("universe_delisted", len(delisted))
        run.event("discovery", delisted_sample=sorted(delisted)[:50])

        log.info(
            "discovery complete: %d listed + %d delisted = %d tickers to backfill",
            len(listed),
            len(delisted),
            len(listed | delisted),
        )
        log.info("next: python scripts/backfill.py --universe delisted")

    return 0


if __name__ == "__main__":
    sys.exit(main())
