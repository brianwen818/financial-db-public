"""Rate limiter tests.

The limiter is what stands between a 6,000-call backfill and an HTTP 402, so
its throttling must actually engage and its state must be shared across
processes rather than per-object.
"""

from __future__ import annotations

import json
import time

import pytest

from finmind_pipeline.utils.rate_limiter import RateLimiter

# 3600/hr == exactly one token per second, which keeps the timing assertions
# fast and unambiguous.
PER_HOUR = 3600


BURST = 20


@pytest.fixture
def limiter(tmp_state) -> RateLimiter:
    return RateLimiter(tmp_state, per_hour=PER_HOUR, burst=BURST)


class TestBurstBound:
    """The bug that broke the first production backfill.

    A bucket refills while it drains, so the most it can admit over a window W
    is burst + rate*W. With burst == rate (the naive choice) that is twice the
    intended hourly budget, which is exactly how a 5,400/hr limiter walked into
    an HTTP 402 against a 6,000/hr cap.
    """

    def test_worst_case_hour_is_bounded_by_burst_plus_rate(self, tmp_state):
        provider_cap, budget = 6000, 5400
        rl = RateLimiter(tmp_state, per_hour=budget, burst=provider_cap - budget)
        assert rl.capacity + rl.per_hour <= provider_cap

    def test_burst_equal_to_rate_would_double_the_budget(self, tmp_state):
        """Documents why the default must not be burst == per_hour."""
        rl = RateLimiter(tmp_state, per_hour=5400, burst=5400)
        assert rl.capacity + rl.per_hour == 10800  # 2x -- the original defect

    def test_default_burst_is_a_small_fraction_of_the_rate(self, tmp_state):
        rl = RateLimiter(tmp_state, per_hour=PER_HOUR)
        assert rl.capacity <= PER_HOUR / 5

    def test_burst_must_be_positive(self, tmp_state):
        with pytest.raises(ValueError):
            RateLimiter(tmp_state, per_hour=PER_HOUR, burst=0)


class TestBucketMechanics:
    def test_starts_at_burst_capacity(self, limiter):
        assert limiter.available() == pytest.approx(BURST, rel=1e-3)

    def test_refill_rate(self, limiter):
        assert limiter.refill_per_second == pytest.approx(1.0)

    def test_acquire_consumes(self, limiter):
        before = limiter.available()
        limiter.acquire(10)
        assert limiter.available() == pytest.approx(before - 10, abs=1.0)

    def test_rejects_request_larger_than_capacity(self, limiter):
        with pytest.raises(ValueError, match="capacity"):
            limiter.acquire(BURST + 1)

    def test_invalid_rate_rejected(self, tmp_state):
        with pytest.raises(ValueError):
            RateLimiter(tmp_state, per_hour=0)


class TestThrottling:
    def test_burst_from_full_bucket_is_not_delayed(self, limiter):
        start = time.monotonic()
        for _ in range(BURST):
            limiter.acquire()
        assert time.monotonic() - start < 1.5

    def test_empty_bucket_blocks_until_refilled(self, limiter):
        limiter._write(0.0, time.time())
        start = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - start
        assert 0.7 < elapsed < 3.0, f"expected ~1s throttle, got {elapsed:.2f}s"

    def test_partial_bucket_waits_only_the_shortfall(self, limiter):
        limiter._write(2.0, time.time())
        start = time.monotonic()
        limiter.acquire(4)  # 2 available, needs 2 more seconds
        elapsed = time.monotonic() - start
        assert 1.5 < elapsed < 4.0


class TestPersistence:
    def test_state_shared_between_instances(self, tmp_state):
        """A backfill and a notebook must draw from one budget, not two."""
        a = RateLimiter(tmp_state, per_hour=PER_HOUR, burst=BURST)
        a.acquire(15)
        b = RateLimiter(tmp_state, per_hour=PER_HOUR, burst=BURST)
        assert b.available() < BURST - 10

    def test_corrupt_state_file_recovers(self, tmp_state):
        tmp_state.parent.mkdir(parents=True, exist_ok=True)
        tmp_state.write_text("{ this is not json", encoding="utf-8")
        limiter = RateLimiter(tmp_state, per_hour=PER_HOUR, burst=BURST)
        assert limiter.available() == pytest.approx(BURST, rel=1e-3)

    def test_missing_state_file_starts_full(self, limiter):
        assert not limiter.state_path.exists()
        limiter.acquire()
        assert limiter.state_path.exists()

    def test_clock_skew_cannot_conjure_tokens(self, tmp_state):
        """A future timestamp must not be treated as elapsed refill time."""
        limiter = RateLimiter(tmp_state, per_hour=PER_HOUR, burst=BURST)
        limiter._write(5.0, time.time() + 10_000)
        assert limiter.available() <= BURST

    def test_capacity_caps_accumulation(self, tmp_state):
        """A long idle period must bank at most one burst, never more."""
        limiter = RateLimiter(tmp_state, per_hour=PER_HOUR, burst=BURST)
        limiter._write(0.0, time.time() - 100_000)
        assert limiter.available() == pytest.approx(BURST, rel=1e-3)

    def test_reset_refills(self, limiter):
        limiter._write(0.0, time.time())
        limiter.reset()
        assert limiter.available() == pytest.approx(BURST, rel=1e-3)

    def test_drain_empties_the_bucket(self, limiter):
        """Called after a 402: the provider's accounting overrides ours."""
        limiter.drain()
        assert limiter.available() < 1.0

    def test_state_is_valid_json(self, limiter):
        limiter.acquire()
        payload = json.loads(limiter.state_path.read_text(encoding="utf-8"))
        assert {"tokens", "updated_at"} <= payload.keys()


class TestLocking:
    def test_lock_released_after_use(self, limiter):
        limiter.acquire()
        assert not limiter.state_path.with_suffix(".json.lock").exists()

    def test_stale_lock_is_reclaimed(self, limiter):
        """A lock orphaned by a killed process must not wedge the pipeline."""
        lock = limiter.state_path.with_suffix(".json.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")
        import os

        old = time.time() - 3600
        os.utime(lock, (old, old))
        limiter.acquire()  # must not hang
        assert not lock.exists()
