"""Thin, rate-limited client for the FinMind v4 API.

Deliberately built on ``requests`` rather than the FinMind SDK: the SDK applies
its own hidden throttling, which would fight the cross-process token bucket in
``rate_limiter``. FinMind's own agent guidance also documents the raw-requests
pattern, so this stays close to the reference behaviour.

Probe findings that shape this module (measured 2026-08-26, Sponsor tier):

* Omitting ``data_id`` returns the whole market — but only for ``start_date``.
  ``end_date`` is silently ignored, so a market call is always exactly one day.
* A per-ticker call has no row cap: ``2330`` returns 8,053 rows spanning
  1994-10-01 to today in a single response.
* HTTP 402 means the hourly quota is exhausted.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from finmind_pipeline.settings import Settings, get_settings
from finmind_pipeline.utils.rate_limiter import RateLimiter, build_rate_limiter

log = logging.getLogger(__name__)


class FinMindError(RuntimeError):
    """A FinMind response that will not become valid by retrying."""


class FinMindQuotaError(FinMindError):
    """HTTP 402 — hourly request quota exhausted."""


class FinMindTransientError(RuntimeError):
    """Network blip or 5xx; worth retrying."""


class FinMindClient:
    def __init__(
        self,
        settings: Settings | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.limiter = limiter if limiter is not None else build_rate_limiter()
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.settings.token}"})
        self.call_count = 0
        self.quota_waits = 0
        self.quota_backoff_seconds = self.settings.quota_backoff_seconds
        self.quota_retries = self.settings.quota_retries
        # Built here rather than as a @retry decorator so that api.max_retries
        # in datasets.yml actually reaches the policy -- a decorator is bound at
        # class-definition time and cannot see instance settings.
        self._retrying = Retrying(
            retry=retry_if_exception_type(FinMindTransientError),
            stop=stop_after_attempt(self.settings.max_retries),
            wait=wait_exponential(multiplier=2, min=2, max=60),
            reraise=True,
        )

    # ------------------------------------------------------------------
    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> FinMindClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    def user_info(self) -> dict[str, Any]:
        """Quota and subscription status. Does not count against the budget."""
        resp = self.session.get(self.settings.user_info_url, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def log_quota(self) -> None:
        try:
            info = self.user_info()
        except Exception as exc:  # noqa: BLE001 - purely informational
            log.warning("could not read quota: %s", exc)
            return
        log.info(
            "FinMind tier=%s hourly_limit=%s used_this_hour=%s local_budget=%s",
            info.get("level_title"),
            info.get("api_request_limit_hour"),
            info.get("user_count"),
            self.settings.rate_budget_per_hour,
        )

    # ------------------------------------------------------------------
    def _request(self, params: dict[str, str]) -> list[dict[str, Any]]:
        """One API call, retrying transient failures per ``api.max_retries``."""
        return self._retrying(self._request_once, params)

    def _request_once(self, params: dict[str, str]) -> list[dict[str, Any]]:
        self.limiter.acquire()
        self.call_count += 1
        try:
            resp = self.session.get(
                self.settings.base_url,
                params=params,
                timeout=self.settings.timeout_seconds,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise FinMindTransientError(f"network error: {exc}") from exc

        if resp.status_code == 402:
            raise FinMindQuotaError(
                "HTTP 402: hourly quota exhausted. "
                f"Tier {self.settings.subscription_plan} allows "
                f"{self.settings.api_hour_limit}/hr."
            )
        if resp.status_code == 429:
            raise FinMindTransientError("HTTP 429: rate limited")
        if resp.status_code >= 500:
            raise FinMindTransientError(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise FinMindError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise FinMindTransientError("non-JSON response") from exc

        if body.get("status") != 200:
            raise FinMindError(
                f"FinMind status={body.get('status')} msg={body.get('msg')!r} "
                f"params={ {k: v for k, v in params.items() if k != 'token'} }"
            )
        return body.get("data") or []

    def _request_with_quota_backoff(self, params: dict[str, str]) -> list[dict[str, Any]]:
        """Wrap ``_request``, surviving a quota rejection instead of dying on it.

        A 402 partway through a 6,000-call backfill should cost time, not the
        whole run. On each rejection the local bucket is drained — so work
        resumes at the sustained refill rate rather than immediately bursting
        into the same wall — and the call is retried after a pause.
        """
        for attempt in range(1, self.quota_retries + 1):
            try:
                return self._request(params)
            except FinMindQuotaError:
                if attempt == self.quota_retries:
                    raise
                self.quota_waits += 1
                # Drop any local burst allowance; the provider disagrees with us
                # about how much is left, and it is the authority.
                try:
                    self.limiter.drain()
                except Exception:  # noqa: BLE001 - never fail on the recovery path
                    pass
                log.warning(
                    "quota rejected (402); backing off %ds then retrying (%d/%d)",
                    self.quota_backoff_seconds,
                    attempt,
                    self.quota_retries - 1,
                )
                time.sleep(self.quota_backoff_seconds)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------
    def fetch(
        self,
        dataset: str,
        *,
        data_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """One API call. Returns the raw ``data`` list, possibly empty.

        An empty list is a normal outcome (non-trading day, delisted ticker,
        index pseudo-code) and is not treated as an error.
        """
        params: dict[str, str] = {"dataset": dataset}
        if data_id is not None:
            params["data_id"] = data_id
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        return self._request_with_quota_backoff(params)

    def fetch_market_day(self, dataset: str, date: str) -> list[dict[str, Any]]:
        """Whole market for a single day.

        ``end_date`` is deliberately omitted: with no ``data_id`` the API
        ignores it and returns only ``start_date``, so passing it would imply a
        range that never materialises.
        """
        return self.fetch(dataset, start_date=date)

    def fetch_ticker_history(
        self,
        dataset: str,
        stock_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Full history for one ticker in a single call."""
        return self.fetch(
            dataset,
            data_id=stock_id,
            start_date=start_date or self.settings.history_start,
            end_date=end_date,
        )
