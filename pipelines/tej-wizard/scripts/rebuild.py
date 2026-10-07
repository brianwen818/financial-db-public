"""Rebuild the processed layer from the raw TEJ workbooks.

Reads no network and calls nothing external, so it is free and safe to run at
any time. Parsing the 120 price workbooks takes about 2.5 minutes and the 48
index-constituent workbooks about 35 seconds; the partition writes take seconds.

This is always a full rebuild, for both datasets. A price workbook is one
security's entire history and an index workbook is one index's entire year, so
a single refreshed file touches every date it covers and there is no meaningful
"incremental" unit below the whole dataset. The staged directory swap keeps the
live data queryable throughout.

Usage::

    python scripts/rebuild.py                      # both datasets
    python scripts/rebuild.py --only prices        # just the price layer
    python scripts/rebuild.py --only index         # just the constituent layer
    python scripts/rebuild.py --tickers 2330,0050  # a price subset, for a quick check
    python scripts/rebuild.py --skip-db            # leave the views alone
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tej_pipeline.database.build import build  # noqa: E402
from tej_pipeline.ingestion.workbook_scan import scan, scan_index  # noqa: E402
from tej_pipeline.processing import index_constituents as ic  # noqa: E402
from tej_pipeline.processing.prices import RawLayerBusyError, rebuild  # noqa: E402
from tej_pipeline.settings import get_settings  # noqa: E402
from tej_pipeline.utils.logging_setup import setup_logging  # noqa: E402
from tej_pipeline.utils.run_manifest import RunManifest  # noqa: E402

INVENTORY_RELPATH = "processed/workbook-inventory/workbook_inventory.parquet"
INDEX_INVENTORY_RELPATH = (
    "processed/index-workbook-inventory/index_workbook_inventory.parquet"
)


def _print_summary(title: str, summary: dict[str, object]) -> None:
    width = max(len(k) for k in summary)
    print(f"\n{title}")
    for key, value in summary.items():
        print(f"  {key:<{width}}  {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        choices=("all", "prices", "index"),
        default="all",
        help="which dataset to rebuild; default is both",
    )
    parser.add_argument(
        "--tickers",
        help="comma-separated stock ids to rebuild the price layer from; "
        "default is every workbook",
    )
    parser.add_argument(
        "--indices",
        help="comma-separated index ids (TWN50,TM100); default is every index",
    )
    parser.add_argument(
        "--skip-db", action="store_true", help="do not rebuild the DuckDB views afterwards"
    )
    args = parser.parse_args()

    setup_logging("processing")
    settings = get_settings()
    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
    indices = [i.strip() for i in args.indices.split(",")] if args.indices else None

    if tickers:
        print(
            "WARNING: --tickers rebuilds the processed layer from a SUBSET of the\n"
            "         workbooks. The staged swap replaces the whole dataset, so every\n"
            "         other security will disappear until a full rebuild runs.\n"
        )
    if indices:
        print(
            "WARNING: --indices rebuilds the constituent layer from a SUBSET of the\n"
            "         indices. The staged swap replaces the whole dataset, so the\n"
            "         other index will disappear until a full rebuild runs.\n"
        )

    do_prices = args.only in ("all", "prices")
    do_index = args.only in ("all", "index")
    summaries: list[tuple[str, dict[str, object]]] = []

    with RunManifest("rebuild") as run:
        if do_prices:
            try:
                summary = rebuild(tickers=tickers)
            except RawLayerBusyError as exc:
                run.error(str(exc))
                print(f"\nERROR: {exc}")
                return 2
            for key, value in summary.items():
                run.metric(key, value)
            summaries.append(("adj_daily_prices", summary))

            inventory = scan(
                settings.raw_dir_for("adj_daily_prices"),
                out_path=settings.db_root / INVENTORY_RELPATH,
            )
            run.metric("inventory_rows", inventory.num_rows)

        if do_index:
            try:
                index_summary = ic.rebuild(indices=indices)
            except ic.RawLayerBusyError as exc:
                run.error(str(exc))
                print(f"\nERROR: {exc}")
                return 2
            for key, value in index_summary.items():
                run.metric(f"index_{key}", value)
            summaries.append(("index_constituents", index_summary))

            index_inventory = scan_index(
                settings.raw_dir_for("index_constituents"),
                out_path=settings.db_root / INDEX_INVENTORY_RELPATH,
            )
            run.metric("index_inventory_rows", index_inventory.num_rows)

        if not args.skip_db:
            build()
            run.metric("views_rebuilt", True)

    for title, summary in summaries:
        _print_summary(title, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
