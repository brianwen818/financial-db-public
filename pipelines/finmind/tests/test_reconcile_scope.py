"""What the weekly reconciliation is allowed to ask about.

The absent/stale checks answer "should this ticker have fresh data?", and
getting their scope wrong is expensive in a way that never surfaces as an
error — just a few hundred wasted calls every week, forever.

Two things put a ticker out of scope, and they are different:

* it has dropped out of the current security master (a normal delisting), and
* the master still lists it but ``TaiwanStockDelisting`` says it is gone.
  TaiwanStockInfo does this for 251 codes, ``9915`` among them, delisted in
  2008 and still listed today.

The rule is reimplemented here rather than imported because it lives in
``scripts/weekly_refresh.py``, which is not an importable package — the same
approach ``test_backfill_resume.py`` takes.
"""

from __future__ import annotations

import datetime as dt

import pytest

from finmind_pipeline.ingestion import tw_delisting


def checkable_for(current, delisted_on, last_seen):
    """The weekly job's scope rule for checks C and D."""
    return {
        s
        for s in current
        if s not in delisted_on
        or (last_seen.get(s) is not None and last_seen[s] > delisted_on[s])
    }


D2008 = dt.date(2008, 3, 1)
D2026 = dt.date(2026, 8, 26)


class TestScopeRule:
    def test_plain_listed_ticker_is_checked(self):
        assert checkable_for({"2330"}, {}, {"2330": D2026}) == {"2330"}

    def test_master_entry_upstream_calls_delisted_is_dropped(self):
        """9915 is in TaiwanStockInfo and delisted since 2008. Checking it
        means re-fetching frozen history every week for ever."""
        assert checkable_for({"9915"}, {"9915": D2008}, {"9915": dt.date(2008, 2, 21)}) == set()

    def test_relisted_ticker_comes_back_into_scope(self):
        """A bar after the recorded delisting date means it trades again."""
        current, delisted = {"1234"}, {"1234": D2008}
        assert checkable_for(current, delisted, {"1234": D2026}) == {"1234"}

    def test_delisted_with_no_price_history_is_dropped(self):
        """last_seen is absent for a code that never returned data; that must
        not read as 'relisted'."""
        assert checkable_for({"1204"}, {"1204": D2008}, {}) == set()

    def test_empty_delisting_log_checks_everything(self):
        """Before the delisting dataset is ingested the rule is a no-op rather
        than a filter that silently drops the whole universe."""
        current = {"2330", "0050"}
        assert checkable_for(current, {}, {}) == current


class TestDelistedDatesLoader:
    def test_returns_a_mapping(self, settings):
        path = (
            settings.path(settings.dataset("tw_delisting").processed) / "tw_delisting.parquet"
        )
        if not path.exists():
            pytest.skip("tw_delisting not ingested yet")
        dates = tw_delisting.load_delisted_dates()
        assert dates
        assert all(isinstance(v, dt.date) for v in dates.values())

    def test_keeps_the_most_recent_event_per_code(self, settings):
        """A code can delist, relist and delist again; the current state is the
        latest event, so the loader must not return the first one."""
        path = (
            settings.path(settings.dataset("tw_delisting").processed) / "tw_delisting.parquet"
        )
        if not path.exists():
            pytest.skip("tw_delisting not ingested yet")

        import pyarrow.parquet as pq

        table = pq.read_table(path, columns=["stock_id", "delisted_date"])
        newest: dict[str, dt.date] = {}
        for sid, d in zip(
            table.column("stock_id").to_pylist(),
            table.column("delisted_date").to_pylist(),
            strict=True,
        ):
            if sid and d is not None and (sid not in newest or d > newest[sid]):
                newest[sid] = d
        assert tw_delisting.load_delisted_dates() == newest

    def test_missing_log_is_empty_not_an_error(self, tmp_path, monkeypatch):
        """A fresh checkout has no processed log; the weekly job must still
        run rather than fail on a file that does not exist yet."""

        class _Dataset:
            processed = "processed/tw-delisting"

        class _Settings:
            def path(self, relative):
                return tmp_path / relative

            def dataset(self, name):
                return _Dataset()

        monkeypatch.setattr(tw_delisting, "get_settings", _Settings)
        assert tw_delisting.load_delisted_dates() == {}
