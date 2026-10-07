"""Seed missing trading dates, then refresh raw TEJ workbooks through Excel.

This is the only part of the pipeline that reaches outside the machine: it
drives the TEJ Smart Wizard add-in, which authenticates as you and pulls
licensed data. It also **overwrites the raw layer in place**, and the raw layer
is this database's source of truth -- there is no API to re-fetch from the way
the FinMind pipeline has. So it takes a confirmation, backs the workbooks up
first, and is never run implicitly by anything else.

Excel runs on a private Windows desktop, so nothing appears on screen and
nothing steals focus. Pass ``--visible`` to watch instead -- do that for the
first supervised run, because an expired TEJ session puts up a login dialog and
a hidden Excel would sit on it until the watchdog killed it.

Two datasets, two different plans:

**prices** (``waprcd1``) -- one workbook per security. ``--scope`` chooses which:
``current-members`` is the ~150 securities in TWN50 or TM100 today and is what
the daily job uses; ``all`` is every workbook and is what the weekly job uses,
because a back-adjusted history is rewritten by every ex-dividend and an
untouched workbook slowly goes stale even though its dates are complete.

**index** (``widxs``) -- one workbook per index-year, and only the current year
moves. The horizon is the *next* session, not today, because a constituent list
dated D is published after D-1's close. Dates past today are seeded as optional:
if TEJ has not published yet the prompt row is dropped and the run still counts
as a success.

Usage::

    python scripts/refresh_workbooks.py --dataset index --dry-run
    python scripts/refresh_workbooks.py --missing-only --dry-run
    python scripts/refresh_workbooks.py --limit 1 --visible    # supervised first run
    python scripts/refresh_workbooks.py --missing-only --yes   # unattended, stale files only
    python scripts/refresh_workbooks.py --scope current-members --yes
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tej_pipeline.ingestion.excel_refresh import (  # noqa: E402
    INDEX_SEED,
    ExcelUnavailableError,
    RefreshJob,
    backup_workbooks,
    run_refresh_jobs,
)
from tej_pipeline.ingestion.trading_dates import (  # noqa: E402
    index_identity,
    load_trading_dates,
    missing_dates_for_workbooks,
    next_session,
    plan_index_seeds,
)
from tej_pipeline.processing import index_constituents as ic  # noqa: E402
from tej_pipeline.processing.prices import (  # noqa: E402
    RawLayerBusyError,
    assert_raw_readable,
    raw_workbooks,
)
from tej_pipeline.settings import get_settings  # noqa: E402
from tej_pipeline.utils.logging_setup import setup_logging  # noqa: E402
from tej_pipeline.utils.run_manifest import RunManifest  # noqa: E402


def _iso_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def plan_price_jobs(args, settings) -> tuple[list[RefreshJob], list[str]]:
    """Build the price refresh jobs. Returns ``(jobs, notes)``."""
    raw_dir = settings.raw_dir_for("adj_daily_prices")
    assert_raw_readable(raw_dir)

    all_paths = raw_workbooks(raw_dir)
    paths = all_paths
    notes: list[str] = []

    if args.scope == "current-members":
        members = ic.current_members()
        paths = [p for p in paths if p.stem in members]
        notes.append(
            f"scope=current-members: {len(paths)} of {len(all_paths)} workbooks "
            f"are in TWN50 or TM100 today"
        )
    if args.tickers:
        wanted = {t.strip() for t in args.tickers.split(",")}
        paths = [p for p in paths if p.stem in wanted]
        missing = wanted - {p.stem for p in paths}
        if missing:
            raise SystemExit(f"ERROR: no workbook for {', '.join(sorted(missing))}")
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        return [], notes

    as_of = args.as_of or dt.date.today()
    trading_dates = load_trading_dates(settings.trading_dates_path, through=as_of)
    seed_dates_by_stock, market_latest = missing_dates_for_workbooks(
        all_paths, paths, trading_dates
    )
    if args.missing_only:
        paths = [p for p in paths if seed_dates_by_stock.get(p.stem)]
    seeded = sum(len(seed_dates_by_stock.get(p.stem, ())) for p in paths)
    notes.append(
        f"trading calendar through {as_of}: raw latest {market_latest}; "
        f"{seeded} missing row(s) to seed"
    )
    jobs = [
        RefreshJob(
            path=p,
            label=p.stem,
            seed_dates=seed_dates_by_stock.get(p.stem, ()),
        )
        for p in paths
    ]
    return jobs, notes


def plan_index_jobs(args, settings) -> tuple[list[RefreshJob], list[str]]:
    """Build the index-constituent refresh jobs. Returns ``(jobs, notes)``."""
    raw_dir = settings.raw_dir_for("index_constituents")
    ic.assert_raw_readable(raw_dir)

    as_of = args.as_of or dt.date.today()
    workbooks = ic.current_year_workbooks(raw_dir, as_of.year)
    known = set(settings.dataset("index_constituents").indices)
    if args.indices:
        wanted = {i.strip() for i in args.indices.split(",")}
        workbooks = {k: v for k, v in workbooks.items() if k in wanted}
        known &= wanted

    # A new calendar year has no workbook until one is made from the add-in.
    # Guessing which file to copy is exactly the kind of thing that quietly
    # produces a workbook holding two years, so this stops instead.
    absent = sorted(known - set(workbooks))
    if absent:
        raise SystemExit(
            f"ERROR: no {as_of.year} workbook for {', '.join(absent)}.\n"
            f"       Create it once from the TEJ add-in and save it as\n"
            f"       {raw_dir / absent[0] / f'{as_of.year}.xlsx'}\n"
            f"       See docs/operations.md, 'Starting a new index year'."
        )
    if not workbooks:
        return [], []

    # Load past today: the horizon is the next session, which by definition is
    # not in a calendar filtered to today.
    trading_dates = load_trading_dates(
        settings.trading_dates_path, through=dt.date(as_of.year + 1, 12, 31)
    )
    horizon = next_session(trading_dates, as_of)
    plans = plan_index_seeds(workbooks, trading_dates, as_of)

    notes = [
        f"as of {as_of}, next session {horizon}: "
        + ", ".join(
            f"{p.index_id} at {p.workbook_latest} "
            f"(+{len(p.required)} required, +{len(p.optional)} optional)"
            for p in plans
        )
    ]
    jobs = [
        RefreshJob(
            path=p.path,
            label=f"{p.index_id}/{p.path.name}",
            seed_dates=p.seed_dates,
            optional_dates=p.optional,
            contract=INDEX_SEED,
            identity=index_identity(p.index_id),
        )
        for p in plans
        if p.seed_dates or not args.missing_only
    ]
    return jobs, notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        choices=("prices", "index"),
        default="prices",
        help="which raw layer to refresh; default is the price workbooks",
    )
    parser.add_argument("--tickers", help="prices: comma-separated stock ids")
    parser.add_argument("--indices", help="index: comma-separated index ids (TWN50,TM100)")
    parser.add_argument(
        "--scope",
        choices=("all", "current-members"),
        default="all",
        help="prices: refresh every workbook, or only current index members",
    )
    parser.add_argument("--limit", type=int, help="refresh at most this many workbooks")
    parser.add_argument(
        "--visible",
        action="store_true",
        help="show the Excel window instead of using a private desktop "
        "(use for the first run)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list what would be refreshed and stop"
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="refresh only workbooks missing one or more trading dates",
    )
    parser.add_argument("--no-backup", action="store_true", help="skip the pre-refresh backup")
    parser.add_argument(
        "--yes", "-y", action="store_true", help="do not ask for confirmation (for scheduled runs)"
    )
    parser.add_argument(
        "--as-of",
        type=_iso_date,
        help="plan seed dates as if today were YYYY-MM-DD; default is today",
    )
    args = parser.parse_args()

    setup_logging("ingestion")
    settings = get_settings()

    try:
        if args.dataset == "index":
            jobs, notes = plan_index_jobs(args, settings)
            raw_root = settings.raw_dir_for("index_constituents")
        else:
            jobs, notes = plan_price_jobs(args, settings)
            raw_root = settings.raw_dir_for("adj_daily_prices")
    except (RawLayerBusyError, ic.RawLayerBusyError) as exc:
        print(f"ERROR: {exc}")
        return 2

    for note in notes:
        print(note)
    if not jobs:
        print(f"nothing to refresh: {args.dataset} workbooks are current")
        return 0

    print(f"\n{len(jobs)} workbook(s) to refresh via {settings.addin.xla_path}")
    for job in jobs[:10]:
        seeded = f"  +{len(job.seed_dates)} seed" if job.seed_dates else ""
        print(f"  {job.label}{seeded}")
    if len(jobs) > 10:
        print(f"  ... and {len(jobs) - 10} more")

    if args.dry_run:
        print("\n--dry-run: nothing was opened or refreshed")
        return 0

    if not args.yes:
        print(
            "\nThis authenticates to TEJ as you and OVERWRITES these workbooks in place."
            "\nProceed? type 'yes' to continue: ",
            end="",
        )
        if input().strip().lower() not in ("yes", "y"):
            print("aborted")
            return 1

    with RunManifest("refresh_workbooks") as run:
        run.metric("dataset", args.dataset)
        if not args.no_backup:
            target = backup_workbooks(
                [j.path for j in jobs],
                settings.temp_dir / "workbook-backup",
                relative_to=raw_root,
            )
            run.metric("backup_dir", str(target))

        try:
            results = run_refresh_jobs(jobs, settings.addin, visible=args.visible)
        except ExcelUnavailableError as exc:
            run.error(str(exc))
            print(f"\nERROR: {exc}")
            return 2

        ok = [r for r in results if r.ok]
        failed = [r for r in results if not r.ok]
        stalled = [r for r in results if r.no_new_data]
        # TEJ returning nothing for one workbook is a security that stopped
        # trading. TEJ returning nothing for ALL of them is an outage or a dead
        # session, and must not be reported as a clean run.
        outage = len(stalled) == len(results) and len(results) > 1
        run.metric("attempted", len(results))
        run.metric("succeeded", len(ok))
        run.metric("failed", len(failed))
        run.metric("no_new_data", len(stalled))
        run.metric("changed", sum(1 for r in ok if r.changed))
        run.metric("seeded_rows", sum(len(r.seeded_dates) for r in results))
        run.metric("seconds_total", round(sum(r.seconds for r in results), 1))
        for r in failed:
            run.error(f"{r.label}: {r.message}")
        if outage:
            run.error(
                f"all {len(results)} workbooks returned no data; "
                "TEJ is unreachable or the add-in's session has expired"
            )
        for r in results:
            run.event(
                "refresh",
                label=r.label,
                stock_id=r.stock_id,
                ok=r.ok,
                seconds=r.seconds,
                bytes_delta=r.bytes_after - r.bytes_before,
                seeded_dates=[d.isoformat() for d in r.seeded_dates],
                latest_date_after=(
                    r.latest_date_after.isoformat() if r.latest_date_after else None
                ),
                no_new_data=r.no_new_data,
                message=r.message,
            )

    print(f"\n{len(ok)}/{len(results)} refreshed, {sum(1 for r in ok if r.changed)} changed")
    if stalled:
        print(
            f"\n{len(stalled)} workbook(s) had no new data and were left unchanged."
            "\nA security that has stopped trading looks exactly like this; check"
            "\nv_workbook_status for any that should have advanced."
        )
        for r in stalled[:10]:
            print(f"  {r.label}: {r.message}")
        if len(stalled) > 10:
            print(f"  ... and {len(stalled) - 10} more")
    if outage:
        print(
            f"\nERROR: all {len(results)} workbooks returned no data. That is not"
            f"\n{len(results)} delistings -- TEJ is unreachable or the add-in's"
            "\nsession has expired."
        )
        return 2
    for r in failed:
        print(f"  FAILED {r.label}: {r.message}")
    if failed:
        print("\nThe raw layer was NOT fully refreshed. Backups are in")
        print(f"  {settings.temp_dir / 'workbook-backup'}")
    print("\nNow run: python scripts/rebuild.py")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
