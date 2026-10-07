"""Ingest ``TaiwanStockPrice`` — unadjusted daily OHLCV.

Thin wrapper binding the shared price ingestion to this dataset; see
``prices.py`` for the mechanics and the fetch-unit rationale.

This series is genuinely append-only: a past day's values never change, so the
daily job only ever adds a new file and the reconciliation only ever has to ask
"which trading days am I missing?".
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from pathlib import Path

from finmind_pipeline.ingestion import prices
from finmind_pipeline.utils.finmind_client import FinMindClient

DATASET_KEY = "daily_prices"


def fetch_market_day(
    client: FinMindClient,
    date: dt.date | str,
    universe: set[str] | None = None,
    overwrite: bool = False,
) -> prices.FetchResult:
    return prices.fetch_market_day(client, DATASET_KEY, date, universe, overwrite)


def fetch_ticker(
    client: FinMindClient,
    stock_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    overwrite: bool = True,
) -> prices.FetchResult:
    return prices.fetch_ticker(client, DATASET_KEY, stock_id, start_date, end_date, overwrite)


def fetch_tickers(
    client: FinMindClient, stock_ids: Iterable[str], **kwargs: object
) -> dict[str, int]:
    return prices.fetch_tickers(client, DATASET_KEY, stock_ids, **kwargs)


def market_day_path(date: dt.date | str) -> Path:
    return prices.market_day_path(DATASET_KEY, date)


def ticker_path(stock_id: str) -> Path:
    return prices.ticker_path(DATASET_KEY, stock_id)


def existing_market_dates() -> set[dt.date]:
    return prices.existing_market_dates(DATASET_KEY)


def existing_tickers() -> set[str]:
    return prices.existing_tickers(DATASET_KEY)
