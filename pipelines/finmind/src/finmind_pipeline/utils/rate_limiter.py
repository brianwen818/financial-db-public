"""Cross-process token-bucket rate limiter for the FinMind API.

State lives in a JSON file under ``db/finmind/state/`` so that a backfill, a
daily job and an ad-hoc notebook all draw from the *same* hourly budget instead
of each assuming it owns the full quota.

The bucket holds ``rate_budget_per_hour`` tokens and refills continuously at
that rate per hour. ``acquire()`` blocks until a token is available.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from finmind_pipeline.utils.fsutil import replace_with_retry

log = logging.getLogger(__name__)

_LOCK_STALE_SECONDS = 60.0
_LOCK_POLL_SECONDS = 0.02


class _FileLock:
    """Minimal cross-process lock built on O_EXCL, which is atomic on Windows."""

    def __init__(self, target: Path, timeout: float = 30.0) -> None:
        self.path = target.with_suffix(target.suffix + ".lock")
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> _FileLock:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                # Reclaim a lock orphaned by a killed process.
                try:
                    if time.time() - self.path.stat().st_mtime > _LOCK_STALE_SECONDS:
                        log.warning("removing stale lock %s", self.path)
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not acquire {self.path}") from None
                time.sleep(_LOCK_POLL_SECONDS)

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)


class RateLimiter:
    """Token bucket persisted to disk.

    Parameters
    ----------
    state_path:
        JSON file holding ``{"tokens": float, "updated_at": epoch_seconds}``.
    per_hour:
        Sustained refill rate — the long-run request budget.
    burst:
        Bucket capacity. **Must be much smaller than** ``per_hour``.

    Why burst and rate must be decoupled
    ------------------------------------
    A bucket refills *while* it is being drained, so the most calls it can
    admit in any window of length ``W`` is ``burst + per_hour * W``. Setting
    ``burst = per_hour`` — the obvious-looking choice — therefore permits up to
    **twice** the intended hourly budget inside a rolling hour.

    That is not theoretical: the first production backfill here set
    ``burst = per_hour = 5400`` against a 6,000/hr cap, burned the full bucket
    in ~20 minutes, kept drawing at the refill rate, and hit HTTP 402 roughly
    41 minutes in.

    So pick ``burst = provider_limit - per_hour``. With a 6,000/hr cap and a
    5,400/hr budget that means a 600-call burst, bounding any rolling hour at
    5,400 + 600 = 6,000.
    """

    def __init__(self, state_path: Path, per_hour: int, burst: int | None = None) -> None:
        if per_hour <= 0:
            raise ValueError("per_hour must be positive")
        self.state_path = Path(state_path)
        self.per_hour = float(per_hour)
        self.capacity = float(burst if burst is not None else max(1, per_hour // 10))
        if self.capacity <= 0:
            raise ValueError("burst must be positive")
        self.refill_per_second = self.per_hour / 3600.0
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    # -- persistence ---------------------------------------------------
    def _read(self) -> tuple[float, float]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            return float(raw["tokens"]), float(raw["updated_at"])
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
            # First run, or a truncated file from a hard kill: start full.
            return self.capacity, time.time()

    def _write(self, tokens: float, updated_at: float) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"tokens": round(tokens, 4), "updated_at": updated_at}),
            encoding="utf-8",
        )
        # Retried: this runs on every API call, and an unguarded replace
        # loses roughly one ticker per 2,000 calls to a transient AV lock.
        replace_with_retry(tmp, self.state_path)

    # -- api -----------------------------------------------------------
    def _try_consume(self, n: float) -> float:
        """Consume ``n`` tokens, or report the wait needed. Returns 0.0 on success."""
        with _FileLock(self.state_path):
            tokens, updated_at = self._read()
            now = time.time()
            # Clock skew or a hand-edited file must not conjure free tokens.
            elapsed = max(0.0, now - updated_at)
            tokens = min(self.capacity, tokens + elapsed * self.refill_per_second)

            if tokens >= n:
                self._write(tokens - n, now)
                return 0.0

            self._write(tokens, now)
            return (n - tokens) / self.refill_per_second

    def acquire(self, n: int = 1) -> None:
        """Block until ``n`` tokens are available, then consume them."""
        if n > self.capacity:
            raise ValueError(f"cannot acquire {n}: bucket capacity is {self.capacity}")
        while True:
            wait = self._try_consume(float(n))
            if wait <= 0.0:
                return
            if wait > 5.0:
                log.info("rate limit reached, sleeping %.1fs", wait)
            time.sleep(min(wait, 5.0))

    def available(self) -> float:
        """Current token count, without consuming anything."""
        with _FileLock(self.state_path):
            tokens, updated_at = self._read()
            elapsed = max(0.0, time.time() - updated_at)
            return min(self.capacity, tokens + elapsed * self.refill_per_second)

    def drain(self) -> None:
        """Empty the bucket, forcing the sustained rate.

        Used after the provider rejects a call for quota: our accounting and
        theirs have diverged, and theirs wins.
        """
        with _FileLock(self.state_path):
            self._write(0.0, time.time())

    def reset(self) -> None:
        """Refill the bucket. For tests and manual recovery only."""
        with _FileLock(self.state_path):
            self._write(self.capacity, time.time())


def build_rate_limiter() -> RateLimiter:
    """Construct the shared limiter from settings.

    Burst is the headroom between the provider cap and our sustained budget,
    so the worst case in any rolling hour is exactly the provider cap.
    """
    from finmind_pipeline.settings import get_settings

    settings = get_settings()
    budget = settings.rate_budget_per_hour
    burst = max(1, settings.api_hour_limit - budget)
    return RateLimiter(settings.rate_limit_state, budget, burst=burst)
