"""Scheduled daily update: refresh what moved today, rebuild, verify.

Entry point for the Windows Task Scheduler job. Stops at the first stage that
fails, because each depends on the last: if the refresh cannot reach TEJ, the
raw workbooks still hold yesterday's data and rebuilding from them would just
reproduce the database that already exists while making the run look successful.

The stages, and why they are in this order:

1. **Index constituents** -- two workbooks, the current year of TWN50 and
   TM100. Fast, and it is what defines the universe for stage 3.
2. **Rebuild the index layer only** -- no TEJ, ~35s. Without it stage 3 would
   plan against yesterday's membership, so a security joining the index today
   would not have its prices refreshed until tomorrow.
3. **Price workbooks for current index members** -- ~150 of the 365, about 50
   minutes. Securities that have left both indices are deliberately skipped
   here and picked up by ``weekly_refresh.py``: their adjusted history is still
   rewritten by every ex-dividend, but nothing needs it same-day.
4. **Rebuild everything, then verify.**

Excel runs on a private desktop throughout, so nothing appears on screen.

Usage::

    python scripts/update.py                  # the scheduled daily run
    python scripts/update.py --skip-refresh   # rebuild from the workbooks as they are
    python scripts/update.py --scope all      # refresh every price workbook instead
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


def build_stages(args) -> list[tuple[str, list[str]]]:
    refresh = str(SCRIPTS / "refresh_workbooks.py")
    rebuild = str(SCRIPTS / "rebuild.py")
    stages: list[tuple[str, list[str]]] = []

    if not args.skip_refresh:
        stages.append(
            (
                "refresh index constituents (2 workbooks)",
                [refresh, "--yes", "--dataset", "index", "--missing-only"],
            )
        )
        stages.append(
            ("rebuild the index layer, so membership is today's", [rebuild, "--only", "index"])
        )
        price = [refresh, "--yes", "--missing-only", "--scope", args.scope]
        if args.tickers:
            price += ["--tickers", args.tickers]
        stages.append((f"refresh price workbooks (scope={args.scope})", price))

    stages.append(("rebuild processed layer and views", [rebuild]))
    stages.append(("verify", [str(SCRIPTS / "verify.py")]))
    return stages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-refresh",
        action="store_true",
        help="do not touch Excel; rebuild from the workbooks as they are on disk",
    )
    parser.add_argument(
        "--scope",
        choices=("current-members", "all"),
        default="current-members",
        help="which price workbooks to refresh; default is current index members",
    )
    parser.add_argument("--tickers", help="restrict the price refresh to these stock ids")
    args = parser.parse_args()

    setup_logging("ingestion")

    with RunManifest("update") as run:
        run.metric("scope", args.scope)
        for name, argv in build_stages(args):
            code, seconds = run_stage(name, argv)
            run.event("stage", name=name, exit_code=code, seconds=seconds)
            if code != 0:
                run.error(f"stage {name!r} exited {code}")
                print(f"\nSTOPPED: {name} exited {code}; later stages were skipped")
                return code
        run.metric("stages", len(build_stages(args)))

    print("\nupdate complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
