"""Shared fixtures.

The golden fixtures are the six Feather files the user captured by hand in
``db/finmind/temp/``. They are real FinMind responses, so they pin the pipeline
against actual upstream behaviour rather than against invented data. Tests skip
rather than fail when they are absent, so the suite still runs on a clean
checkout with no data.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.feather as feather
import pytest

from finmind_pipeline.settings import get_settings


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def temp_dir(settings) -> Path:
    return settings.temp_dir


def _load(temp_dir: Path, name: str) -> pa.Table:
    path = temp_dir / name
    if not path.exists():
        pytest.skip(f"golden fixture missing: {path}")
    return feather.read_table(path)


@pytest.fixture(scope="session")
def golden_daily(temp_dir) -> pa.Table:
    return _load(temp_dir, "daily_price_example_0050.feather")


@pytest.fixture(scope="session")
def golden_adj(temp_dir) -> pa.Table:
    return _load(temp_dir, "adj_price_example_0050.feather")


@pytest.fixture(scope="session")
def golden_stock_info(temp_dir) -> pa.Table:
    return _load(temp_dir, "tw_stock_info_example.feather")


@pytest.fixture(scope="session")
def golden_industry(temp_dir) -> pa.Table:
    return _load(temp_dir, "tw_industry_example.feather")


@pytest.fixture(scope="session")
def golden_calendar(temp_dir) -> pa.Table:
    return _load(temp_dir, "tw_trading_date_example.feather")


@pytest.fixture(scope="session")
def golden_calendar_expanded(temp_dir) -> pa.Table:
    return _load(temp_dir, "tw_trading_date_expanded_example.feather")


@pytest.fixture
def tmp_state(tmp_path) -> Path:
    return tmp_path / "state.json"
