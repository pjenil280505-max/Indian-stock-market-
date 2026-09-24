"""Same-day loading and NSE publication timing.

The pipeline now attempts the CURRENT trading day, which means it can legitimately
arrive before NSE has published. Mis-handling that in the permanent direction
silently loses a trading day forever, because a settled date is never re-fetched.
"""
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import PUBLICATION_GRACE_DAYS  # noqa: E402
from src.pipeline import NSE_SOURCE, classify_loaded, classify_missing, outstanding_dates  # noqa: E402


@dataclass
class Bar:
    symbol: str = "X"
    deliv_pct: float | None = 40.0


class FakeRepo:
    def __init__(self, settled=None):
        self._settled = set(settled or [])

    def settled_dates(self, source):
        return set(self._settled)


TODAY = date(2026, 9, 24)  # Thursday


class TestTodayIsIncluded:
    def test_current_trading_day_is_attempted(self):
        """The whole point: the evening run must report the session that just
        closed, not the previous one."""
        pending = outstanding_dates(FakeRepo(), NSE_SOURCE, TODAY, catchup_days=5)
        assert TODAY in pending

    def test_today_is_skipped_once_settled(self):
        repo = FakeRepo([TODAY])
        assert TODAY not in outstanding_dates(repo, NSE_SOURCE, TODAY, catchup_days=5)

    def test_opt_out_still_available(self):
        pending = outstanding_dates(
            FakeRepo(), NSE_SOURCE, TODAY, catchup_days=5, include_today=False)
        assert TODAY not in pending

    def test_weekend_today_yields_no_today_entry(self):
        saturday = date(2026, 9, 26)
        pending = outstanding_dates(FakeRepo(), NSE_SOURCE, saturday, catchup_days=3)
        assert saturday not in pending

    def test_still_catches_up_older_dates(self):
        pending = outstanding_dates(FakeRepo(), NSE_SOURCE, TODAY, catchup_days=7)
        assert date(2026, 9, 22) in pending and date(2026, 9, 23) in pending


class TestMissingFileClassification:
    def test_todays_missing_file_is_retryable(self):
        """Not yet published - must NOT settle as a holiday."""
        assert classify_missing(TODAY, TODAY) == "failed"

    def test_recent_missing_file_is_retryable(self):
        assert classify_missing(date(2026, 9, 23), TODAY) == "failed"

    @pytest.mark.parametrize("age", range(0, PUBLICATION_GRACE_DAYS + 1))
    def test_everything_inside_the_grace_window_retries(self, age):
        from datetime import timedelta
        assert classify_missing(TODAY - timedelta(days=age), TODAY) == "failed"

    def test_old_missing_file_is_a_holiday(self):
        """Beyond the grace window it really is a non-trading day."""
        assert classify_missing(date(2026, 8, 15), TODAY) == "no_data"

    def test_boundary_is_exclusive_beyond_grace(self):
        from datetime import timedelta
        just_outside = TODAY - timedelta(days=PUBLICATION_GRACE_DAYS + 1)
        assert classify_missing(just_outside, TODAY) == "no_data"


class TestDeliveryCompleteness:
    def test_bars_with_delivery_are_complete(self):
        assert classify_loaded([Bar(deliv_pct=40.0)], TODAY, TODAY) == "loaded"

    def test_bars_without_delivery_today_are_partial(self):
        """Bhavcopy publishes before sec_bhavdata_full; keep the bars, revisit."""
        assert classify_loaded([Bar(deliv_pct=None)], TODAY, TODAY) == "partial"

    def test_partial_only_needs_one_symbol_with_delivery(self):
        bars = [Bar(deliv_pct=None), Bar(deliv_pct=55.0)]
        assert classify_loaded(bars, TODAY, TODAY) == "loaded"

    def test_old_date_without_delivery_is_accepted_as_final(self):
        """Retrying forever would leave a permanent open item."""
        assert classify_loaded([Bar(deliv_pct=None)], date(2026, 8, 1), TODAY) == "loaded"

    def test_bars_lacking_the_attribute_entirely(self):
        class Minimal:
            symbol = "X"
        assert classify_loaded([Minimal()], TODAY, TODAY) == "partial"


class TestCronSchedule:
    @staticmethod
    def _crons():
        # Parsed with a regex, not PyYAML: CI installs only requirements*.txt,
        # and a yaml import here kept CI red from 802558f until Phase 1a while
        # passing locally wherever PyYAML happened to be installed.
        text = (Path(__file__).resolve().parents[1]
                / ".github/workflows/daily-data-update.yml").read_text()
        return re.findall(r"^\s*-\s*cron:\s*'([^']+)'", text, re.MULTILINE)

    def test_two_fires_per_trading_day(self):
        assert len(self._crons()) == 2

    def test_never_on_the_top_of_the_hour(self):
        """GitHub delays scheduled runs worst at :00; a 4h48m delay was
        observed on the old minute-0 schedule."""
        for cron in self._crons():
            assert cron.split()[0] != "0", f"{cron} fires on the hour"

    def test_weekdays_only(self):
        for cron in self._crons():
            assert cron.split()[4] == "1-5"

    def test_first_fire_is_after_the_market_close(self):
        """15:30 IST close = 10:00 UTC. Firing before that fetches nothing."""
        for cron in self._crons():
            minute, hour = int(cron.split()[0]), int(cron.split()[1])
            assert hour * 60 + minute > 10 * 60, f"{cron} is before the close"

    def test_fires_are_separated_enough_to_enrich(self):
        times = sorted(int(c.split()[1]) * 60 + int(c.split()[0]) for c in self._crons())
        assert times[1] - times[0] >= 120, "safety net too close to the first attempt"
