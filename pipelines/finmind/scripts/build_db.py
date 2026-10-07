"""(Re)build finmind.duckdb from the SQL view definitions.

The database holds only views over Parquet, so this is always safe to re-run
and the file can be deleted at any time and regenerated.

Usage::

    python scripts/build_db.py
    python scripts/build_db.py --verify-only
"""

from __future__ import annotations

import argparse
import logging
import sys

from finmind_pipeline.database import build as db_build
from finmind_pipeline.utils.logging_setup import setup_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--verify-only", action="store_true", help="do not rebuild, just query each view"
    )
    args = ap.parse_args(argv)

    setup_logging("processing")
    logging.getLogger().setLevel(logging.INFO)

    if not args.verify_only:
        db_build.build()

    print(f"\n{'view':<28} {'rows':>14}")
    print("-" * 44)
    failures = 0
    for view, count in db_build.verify().items():
        if isinstance(count, str):
            failures += 1
            print(f"{view:<28} {count}")
        else:
            print(f"{view:<28} {count:>14,}")
    if failures:
        print(f"\n{failures} view(s) failed to query")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
