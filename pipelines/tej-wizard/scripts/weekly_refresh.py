"""Weekly full refresh: re-pull every workbook, rebuild, verify.

The daily job only touches securities that are in TWN50 or TM100 today. This
one touches everything, and it exists because **a back-adjusted history is not
append-only**. Every ex-dividend and every split rewrites the whole series, so
a workbook whose dates are complete can still be wrong: it holds the adjustment
basis from the last time it was refreshed.

A security that has left both indices is exactly the case the daily job skips
and this one catches. Its dates stop moving, so ``v_workbook_status`` cannot
tell you it went stale -- only re-pulling can.

The index-constituent workbooks are refreshed here without seeding too, which
re-pulls the whole current year rather than only the dates it is missing. TEJ
restates constituent weights after the fact; a seeded refresh would only ever
add new rows.

This mirrors the FinMind pipeline's ``weekly_refresh.py``, for the same reason
and on the same cadence.

Runtime: since the 2026-09-28 company-info expansion there are 2,453 price
workbooks at ~10-14s each, an estimated ~8 hours, so schedule it where a long
run of Excel does not matter. Only current index members are refreshed daily,
so for the other ~1,840 still-trading companies this job is the only update. Excel runs on a private desktop,
so it does not interrupt anything if the machine is in use.

Usage::

    python scripts/weekly_refresh.py            # the scheduled Sunday run
    python scripts/weekly_refresh.py --dry-run  # what it would touch
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parents[0] / "src"))

from tej_pipeline.utils.logging_setup import setup_logging  # noqa: E402
from tej_pipeline.utils.run_manifest import RunManifest  # noqa: E402


def run_stage(name: str, argv: list[str]) -> tuple[int, float]:
    print(f"\n{'=' * 70}\n  {name}\n{'=' * 70}")
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, *argv], cwd=SCRIPTS.parent)
    return proc.returncode, round(time.monotonic() - t0, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="list what would be refreshed and stop"
    )
    args = parser.parse_args()

    setup_logging("ingestion")
    refresh = str(SCRIPTS / "refresh_workbooks.py")
    common = ["--yes"] + (["--dry-run"] if args.dry_run else [])

    stages = [
        (
            "re-pull index constituents in full",
            [refresh, *common, "--dataset", "index"],
        ),
        (
            "re-pull every price workbook (adjustment basis, not just dates)",
            [refresh, *common, "--scope", "all"],
        ),
    ]
    if not args.dry_run:
        stages.append(("rebuild processed layer and views", [str(SCRIPTS / "rebuild.py")]))
        stages.append(("verify", [str(SCRIPTS / "verify.py")]))

    with RunManifest("weekly_refresh") as run:
        for name, argv in stages:
            code, seconds = run_stage(name, argv)
            run.event("stage", name=name, exit_code=code, seconds=seconds)
            if code != 0:
                run.error(f"stage {name!r} exited {code}")
                print(f"\nSTOPPED: {name} exited {code}; later stages were skipped")
                return code
        run.metric("stages", len(stages))

    print("\nweekly refresh complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
