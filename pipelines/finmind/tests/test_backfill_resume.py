"""Resume semantics for the backfill checkpoint.

A ticker that failed transiently is still recorded in the checkpoint. If resume
treated "recorded" as "done", that ticker would be silently lost forever -- which
is exactly what happened to four tickers during the first production run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backfill  # noqa: E402


def pending_for(universe, done):
    """Mirror of the selection logic in backfill.main."""
    return [t for t in universe if t not in done or done[t] == -1]


class TestPendingSelection:
    UNIVERSE = ["0050", "2330", "2023", "6469", "1230"]

    def test_untouched_tickers_are_pending(self):
        assert pending_for(self.UNIVERSE, {}) == self.UNIVERSE

    def test_successful_tickers_are_skipped(self):
        done = {"0050": 5697, "2330": 8053}
        assert pending_for(self.UNIVERSE, done) == ["2023", "6469", "1230"]

    def test_failed_tickers_are_retried(self):
        """-1 means failed; it must come back round."""
        done = {"0050": 5697, "2023": -1, "6469": -1}
        assert "2023" in pending_for(self.UNIVERSE, done)
        assert "6469" in pending_for(self.UNIVERSE, done)

    def test_legitimately_empty_tickers_are_not_retried(self):
        """0 rows is a real answer (delisted code); retrying wastes quota."""
        done = {"1230": 0}
        assert "1230" not in pending_for(self.UNIVERSE, done)

    def test_nothing_pending_when_all_succeeded(self):
        done = dict.fromkeys(self.UNIVERSE, 100)
        assert pending_for(self.UNIVERSE, done) == []


class TestCheckpointIO:
    def test_round_trip(self, tmp_path):
        p = tmp_path / "cp.json"
        state = {"daily_prices": {"0050": 5697, "2023": -1}}
        backfill.save_checkpoint(p, state)
        assert backfill.load_checkpoint(p) == state

    def test_missing_file_gives_empty_state(self, tmp_path):
        assert backfill.load_checkpoint(tmp_path / "nope.json") == {}

    def test_corrupt_file_gives_empty_state(self, tmp_path):
        p = tmp_path / "cp.json"
        p.write_text("{ truncated", encoding="utf-8")
        assert backfill.load_checkpoint(p) == {}

    def test_write_is_atomic_no_tmp_left(self, tmp_path):
        p = tmp_path / "cp.json"
        backfill.save_checkpoint(p, {"a": {"b": 1}})
        assert list(tmp_path.glob("*.tmp")) == []
        assert json.loads(p.read_text(encoding="utf-8")) == {"a": {"b": 1}}
