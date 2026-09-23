"""Backfill budget: stop cleanly before a wall, never mid-write.

Two walls matter - the GitHub Actions job timeout and the database free
tier. Stopping short of both keeps the ingestion log consistent, so the next
run resumes exactly where this one stopped.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backfill import DEFAULT_STOP_AT_FRACTION, FREE_TIER_BYTES, Budget  # noqa: E402


class FakeRepo:
    def __init__(self, size=0):
        self.size = size
        self.size_queries = 0

    def database_size_bytes(self):
        self.size_queries += 1
        return self.size


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class TestTimeBudget:
    def test_not_exhausted_before_the_deadline(self):
        clock = FakeClock()
        budget = Budget(10, FakeRepo(), 0, clock=clock)
        clock.advance(9 * 60)
        assert budget.exhausted() is None

    def test_exhausted_at_the_deadline(self):
        clock = FakeClock()
        budget = Budget(10, FakeRepo(), 0, clock=clock)
        clock.advance(10 * 60)
        assert "time budget" in budget.exhausted()

    def test_zero_minutes_means_no_time_limit(self):
        clock = FakeClock()
        budget = Budget(0, FakeRepo(), 0, clock=clock)
        clock.advance(10_000 * 60)
        assert budget.exhausted() is None


class TestStorageBudget:
    def test_stops_when_the_cap_is_reached(self):
        repo = FakeRepo(size=500_000_000)
        budget = Budget(0, repo, 425_000_000, clock=FakeClock())
        reasons = [budget.exhausted() for _ in range(10)]
        assert any(r and "storage budget" in r for r in reasons)

    def test_continues_below_the_cap(self):
        repo = FakeRepo(size=100_000_000)
        budget = Budget(0, repo, 425_000_000, clock=FakeClock())
        assert all(budget.exhausted() is None for _ in range(30))

    def test_size_is_not_queried_on_every_date(self):
        """The guard must be cheap enough to leave switched on."""
        repo = FakeRepo(size=1)
        budget = Budget(0, repo, 425_000_000, clock=FakeClock())
        for _ in range(30):
            budget.exhausted()
        assert repo.size_queries == 3, "expected one size query per 10 dates"

    def test_zero_cap_disables_the_guard(self):
        repo = FakeRepo(size=10**12)
        budget = Budget(0, repo, 0, clock=FakeClock())
        for _ in range(30):
            assert budget.exhausted() is None
        assert repo.size_queries == 0


class TestDefaults:
    def test_default_cap_leaves_headroom_under_the_free_tier(self):
        cap = FREE_TIER_BYTES * DEFAULT_STOP_AT_FRACTION
        assert cap < FREE_TIER_BYTES
        assert FREE_TIER_BYTES - cap >= 50e6, "expect at least 50 MB of headroom"

    def test_free_tier_constant_matches_neon(self):
        assert FREE_TIER_BYTES == pytest.approx(0.5 * 1000**3)


class TestOrdering:
    def test_backfill_attempts_newest_dates_first(self):
        """If the budget runs out, recent history should already be present."""
        source = (Path(__file__).resolve().parents[1] / "scripts" / "backfill.py").read_text()
        assert "reverse=True" in source, "pending dates must be newest-first"
