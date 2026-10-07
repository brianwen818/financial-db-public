"""Build price workbooks for index members that do not have one yet.

The price layer is one TEJ workbook per security, and the original 120 were
downloaded by hand from the add-in. `v_index_universe` now says which index
members are missing: 246 of the 365 securities that have ever been in TWN50 or
TM100, measured 2026-09-05. Downloading those by hand is not reasonable.

There is no way to *compose* a TEJ query from outside the add-in, because the
query definition lives in the workbook's A1 comment. But the definition does not
name the security -- the identity is in column A of every data row. So this
copies an existing workbook, rewrites column A, clears the data, and refreshes.

Verified against a workbook downloaded by hand: rebuilding 1103 from 1101's
produced **230,796 of 230,796 cells identical**, same keys in both directions.
TEJ also trims the date skeleton to the security's real life, so a 2024 listing
built from a 2000-2026 template comes back with only its own 431 rows.

Each workbook is built on a temporary path and moved into `raw/` only after it
validates, so a failure never leaves a half-built file in the source of truth.

Usage::

    python scripts/create_workbooks.py --dry-run
    python scripts/create_workbooks.py --tickers 2331,2363,2376 --visible
    python scripts/create_workbooks.py --limit 5 --yes
    python scripts/create_workbooks.py --yes            # all 246, ~1 hour
    python scripts/create_workbooks.py --tickers-file ../../db/tej-wizard/state/company_universe_tickers.txt --limit 400 --yes
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tej_pipeline.ingestion.excel_refresh import (  # noqa: E402
    ExcelSession,
    ExcelUnavailableError,
)
from tej_pipeline.ingestion.trading_dates import (  # noqa: E402
    latest_workbook_date,
    load_trading_dates,
)
from tej_pipeline.ingestion.workbook_scan import scan_workbook  # noqa: E402
from tej_pipeline.processing import index_constituents as ic  # noqa: E402
from tej_pipeline.processing.prices import (  # noqa: E402
    RawLayerBusyError,
    assert_raw_readable,
    raw_workbooks,
)
from tej_pipeline.settings import get_settings  # noqa: E402
from tej_pipeline.utils.logging_setup import setup_logging  # noqa: E402
from tej_pipeline.utils.run_manifest import RunManifest  # noqa: E402


def pick_template(raw_dir: Path) -> Path:
    """The workbook with the longest history, as the date skeleton to reuse.

    Longest wins because TEJ trims dates a security did not trade but cannot
    invent ones the skeleton does not offer. Starting from the deepest history
    means one template serves every security, whenever it listed.
    """
    books = raw_workbooks(raw_dir)
    if not books:
        raise SystemExit(f"ERROR: no price workbooks in {raw_dir} to use as a template")
    scanned = [(scan_workbook(p)["declared_rows"] or 0, p) for p in books]
    rows, path = max(scanned, key=lambda pair: pair[0])
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    which = parser.add_mutually_exclusive_group()
    which.add_argument(
        "--tickers",
        help="comma-separated stock ids; default is every index member with no workbook",
    )
    which.add_argument(
        "--tickers-file",
        type=Path,
        help="file with one stock id per line, e.g. from list_company_tickers.py",
    )
    parser.add_argument("--limit", type=int, help="build at most this many workbooks")
    parser.add_argument("--template", help="stock id of the workbook to copy the skeleton from")
    parser.add_argument(
        "--visible",
        action="store_true",
        help="show the Excel window instead of using a private desktop",
    )
    parser.add_argument("--dry-run", action="store_true", help="list what would be built and stop")
    parser.add_argument(
        "--yes", "-y", action="store_true", help="do not ask for confirmation"
    )
    parser.add_argument(
        "--as-of",
        type=dt.date.fromisoformat,
        help="build history through YYYY-MM-DD; default is today",
    )
    args = parser.parse_args()

    setup_logging("ingestion")
    settings = get_settings()
    raw_dir = settings.raw_dir_for("adj_daily_prices")

    try:
        assert_raw_readable(raw_dir)
    except RawLayerBusyError as exc:
        print(f"ERROR: {exc}")
        return 2

    existing = {p.stem for p in raw_workbooks(raw_dir)}
    if args.tickers or args.tickers_file:
        raw_ids = (
            args.tickers.split(",")
            if args.tickers
            else args.tickers_file.read_text(encoding="utf-8").splitlines()
        )
        wanted = [t.strip() for t in raw_ids if t.strip()]
        names = dict(ic.members_without_prices())
        targets = [(t, names.get(t, "")) for t in wanted]
    else:
        targets = ic.members_without_prices()

    already = [t for t, _ in targets if t in existing]
    targets = [(t, n) for t, n in targets if t not in existing]
    if already:
        print(f"skipping {len(already)} that already have a workbook: {', '.join(already[:5])}")
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("nothing to build")
        return 0

    template = (
        raw_dir / f"{args.template}.xlsx" if args.template else pick_template(raw_dir)
    )
    if not template.exists():
        print(f"ERROR: template {template} does not exist")
        return 2

    as_of = args.as_of or dt.date.today()
    template_latest = latest_workbook_date(template)
    trading_dates = load_trading_dates(settings.trading_dates_path, through=as_of)
    extra_dates = tuple(d for d in trading_dates if d > template_latest)

    print(f"template {template.name}: history through {template_latest}")
    print(f"seeding {len(extra_dates)} further trading date(s) through {as_of}")
    print(f"\n{len(targets)} workbook(s) to build into {raw_dir}")
    for stock_id, name in targets[:10]:
        print(f"  {stock_id} {name}")
    if len(targets) > 10:
        print(f"  ... and {len(targets) - 10} more")
    print(f"\nestimated {len(targets) * 15 / 60:.0f} minutes at ~15s each")

    if args.dry_run:
        print("\n--dry-run: nothing was opened or built")
        return 0

    if not args.yes:
        print(
            "\nThis authenticates to TEJ as you and WRITES NEW FILES into the raw layer."
            "\nProceed? type 'yes' to continue: ",
            end="",
        )
        if input().strip().lower() not in ("yes", "y"):
            print("aborted")
            return 1

    built, failed = [], []
    with RunManifest("create_workbooks") as run:
        run.metric("template", template.name)
        run.metric("targets", len(targets))
        try:
            with ExcelSession(settings.addin, visible=args.visible) as session:
                for i, (stock_id, name) in enumerate(targets, 1):
                    out_path = raw_dir / f"{stock_id}.xlsx"
                    print(f"[{i}/{len(targets)}] {stock_id} {name} ...", end="", flush=True)
                    result = session.create_from_template(
                        template=template,
                        out_path=out_path,
                        stock_id=stock_id,
                        extra_dates=extra_dates,
                        timeout=settings.addin.timeout_seconds,
                    )
                    if result.ok:
                        built.append(result)
                        print(
                            f" {result.seconds}s, through {result.latest_date_after},"
                            f" {result.bytes_after / 1024:.0f} KiB"
                        )
                    else:
                        failed.append((stock_id, result.message))
                        print(f" FAILED: {result.message}")
                    run.event(
                        "create",
                        stock_id=stock_id,
                        ok=result.ok,
                        seconds=result.seconds,
                        bytes_after=result.bytes_after,
                        latest_date_after=(
                            result.latest_date_after.isoformat()
                            if result.latest_date_after
                            else None
                        ),
                        message=result.message,
                    )
                    # A timeout killed Excel; every later build fails the same way.
                    if result.message == "timed out":
                        break
                    if settings.addin.pause_seconds and i < len(targets):
                        time.sleep(settings.addin.pause_seconds)
        except ExcelUnavailableError as exc:
            run.error(str(exc))
            print(f"\nERROR: {exc}")
            return 2

        run.metric("built", len(built))
        run.metric("failed", len(failed))
        for stock_id, message in failed:
            run.error(f"{stock_id}: {message}")

    print(f"\n{len(built)}/{len(built) + len(failed)} built")
    for stock_id, message in failed:
        print(f"  FAILED {stock_id}: {message}")
    print("\nNow run: python scripts/rebuild.py")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
