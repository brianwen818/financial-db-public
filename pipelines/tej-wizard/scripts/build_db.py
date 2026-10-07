"""(Re)build tej_wizard.duckdb from the SQL view definitions.

The database holds only views over Parquet, so this is always safe to re-run
and the file can be deleted at any time and regenerated.

Usage::

    python scripts/build_db.py
    python scripts/build_db.py --verify-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tej_pipeline.database.build import build, verify  # noqa: E402
from tej_pipeline.settings import get_settings  # noqa: E402
from tej_pipeline.utils.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="query every view without rebuilding, and print the row counts",
    )
    args = parser.parse_args()

    setup_logging("processing")
    settings = get_settings()

    if not args.verify_only:
        build()

    results = verify()
    width = max((len(name) for name in results), default=0)
    failed = 0
    for name, value in results.items():
        if isinstance(value, str):
            failed += 1
            print(f"  {name:<{width}}  {value}")
        else:
            print(f"  {name:<{width}}  {value:>12,}")
    print(f"\n{len(results)} views in {settings.duckdb_path}")
    if failed:
        print(f"{failed} view(s) failed to read")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
