"""Measured numbers for the documents, read from outputs/tables/*.csv.

Nothing here is typed in by hand: the deck and the report quote what
analysis/01_db_snapshot.py and 02_source_comparison.py last wrote. ``before``
holds the same tables as they stood before the 2026-10-04 dedupe fix.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parents[1] / "outputs"
ROOT = Path(__file__).resolve().parents[2]            # repo root
DELIVERABLES = ROOT / "docs"


def table(name: str, before: bool = False) -> pd.DataFrame:
    folder = OUT / ("tables_before_fix" if before else "tables")
    return pd.read_csv(folder / f"{name}.csv")


def row(name: str, before: bool = False) -> pd.Series:
    """The single row of a one-row table."""
    return table(name, before).iloc[0]


def n(value) -> str:
    """12345 -> '12,345'."""
    return f"{int(round(float(value))):,}"


def wan(value, digits: int = 0) -> str:
    """11751428 -> '1,175 萬'."""
    return f"{float(value) / 1e4:,.{digits}f} 萬"


def pct(value, digits: int = 1) -> str:
    return f"{float(value):.{digits}f}%"


# Counts read off the tools' own output on 2026-10-04 (pytest -q, verify.py).
TESTS = {"finmind": 210, "tej": 76}
CHECKS = {"finmind": 28, "tej": 35}
VIEWS = {"finmind": 19, "tej": 20}


class Facts:
    """Named figures used in more than one place."""

    def __init__(self) -> None:
        px = table("snapshot_price_tables").set_index("view_name")
        self.px = px
        self.fm_raw = px.loc["finmind.v_daily_prices"]
        self.fm_adj = px.loc["finmind.v_adj_daily_prices"]
        self.tej_adj = px.loc["tej.v_adj_daily_prices"]
        self.tej_idx = px.loc["tej.v_index_constituents"]
        self.total_rows = int(px["n_rows"].sum())

        self.idx = table("snapshot_index_constituents").set_index("index_id")
        self.tej_universe = table("snapshot_tej_universe").set_index("is_current")
        self.tej_current = int(self.tej_universe.loc[True, "securities"])
        self.tej_stopped = int(self.tej_universe.loc[False, "securities"])
        self.delisted = row("snapshot_finmind_delisted_coverage")
        fm_uni = table("snapshot_finmind_universe")
        self.fm_universe = int(fm_uni["securities"].sum())
        self.health = row("snapshot_health")

        self.overlap = row("cmp_universe_overlap")
        self.overlap_size = row("cmp_overlap_size")
        agree = table("cmp_return_agreement")
        listed = agree[agree["segment"].str.startswith("listed")]
        self.clean = listed[listed["both_days_traded"]].iloc[0]
        self.causes = table("cmp_disagreement_causes")
        self.causes["key"] = self.causes["cause"].str[0]
        self.cause = self.causes.set_index("key")
        self.limit = row("cmp_beyond_limit")
        self.halts = row("cmp_halt_resumptions")
        self.gap_all = table("cmp_total_return_gap").set_index("pair")
        self.gap_2008 = row("cmp_total_return_gap_since_2008")
        self.no_trade = row("cmp_no_trade_disagreements")
        self.tej_suspicious = row("cmp_tej_suspicious_summary")
        self.tej_internal = table("cmp_tej_internal_return_check").set_index("segment")
        self.zero = table("cmp_finmind_zero_price_by_year")
        self.actions = table("cmp_unadjusted_actions_by_market_year")
        self.coverage = table("cmp_coverage_by_year")
        self.unit = table("cmp_volume_unit_error")
        self.latest = row("cmp_latest_common_date")
        level = table("cmp_level_by_year")
        self.level = level
        self.level_within_1c = float((level["n"] * level["close_within_1c_pct"]).sum() / level["n"].sum())
        self.tej_gaps = int(self.health.tej_gap_rows)

        # The dedupe bug, as measured before it was fixed.
        self.stale = row("cmp_stale_market_snapshots", before=True)
        self.case_6669 = table("case_6669_stale_basis", before=True)
        self.limit_before = row("cmp_beyond_limit", before=True)
        causes_before = table("cmp_disagreement_causes", before=True)
        self.pipeline_days_before = int(
            causes_before[causes_before["cause"].str.startswith("B")]["days"].sum()
        )

    def cause_days(self, key: str) -> int:
        return int(self.cause.loc[key, "days"]) if key in self.cause.index else 0
