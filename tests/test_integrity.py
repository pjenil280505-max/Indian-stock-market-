"""Integrity checks: corporate actions, duplicates, missing data, sanity."""
from dataclasses import dataclass
from datetime import date

import pytest

from src.integrity import (
    Finding,
    candidate_dates,
    check_ohlc_sane,
    check_volume_adjustment_consistency,
    cross_source_findings,
    find_duplicates,
    has_errors,
    missing_trading_dates,
    price_adjustment_factor,
    validate_batch,
)


@dataclass
class Bar:
    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    series: str = "EQ"


def bar(symbol="X", d=date(2026, 9, 18), o=100.0, h=110.0, lo=95.0, c=105.0, v=1000):
    return Bar(symbol, d, o, h, lo, c, v)


class TestCorporateActionAdjustment:
    """The Phase 0 finding: Upstox back-adjusts, NSE bhavcopy is raw."""

    def test_reliance_one_for_one_bonus_measured_values(self):
        """Real values: NSE 25-Oct-2024 close 2655.70 vs Upstox 1327.85."""
        assert price_adjustment_factor(2655.70, 1327.85) == pytest.approx(2.0)

    def test_volume_scales_inversely_under_adjustment(self):
        """Prices halve, volume doubles - both measured exactly in Phase 0."""
        factor, ok = check_volume_adjustment_consistency(
            raw_close=2655.70, adj_close=1327.85,
            raw_volume=9_298_748, adj_volume=18_597_496,
        )
        assert factor == pytest.approx(2.0)
        assert ok, "volume must scale inversely to price"

    def test_flags_volume_that_does_not_scale_inversely(self):
        """Price adjusted but volume unchanged means the pair is inconsistent."""
        factor, ok = check_volume_adjustment_consistency(
            raw_close=2655.70, adj_close=1327.85,
            raw_volume=9_298_748, adj_volume=9_298_748,
        )
        assert factor == pytest.approx(2.0)
        assert not ok

    def test_unadjusted_day_has_factor_one(self):
        factor, ok = check_volume_adjustment_consistency(100.0, 100.0, 5000, 5000)
        assert factor == pytest.approx(1.0)
        assert ok

    def test_five_for_one_split(self):
        factor, ok = check_volume_adjustment_consistency(500.0, 100.0, 1000, 5000)
        assert factor == pytest.approx(5.0)
        assert ok

    def test_zero_price_is_uncomputable(self):
        assert price_adjustment_factor(0.0, 100.0) is None
        assert price_adjustment_factor(100.0, 0.0) is None

    def test_negative_price_is_uncomputable(self):
        assert price_adjustment_factor(-5.0, 100.0) is None


class TestCrossSourceFindings:
    def test_clean_pair_produces_factor_and_no_error(self):
        rows = [(1, 2655.70, 1327.85, 9_298_748, 18_597_496)]
        findings, factors = cross_source_findings(rows, date(2024, 10, 25))
        assert not has_errors(findings)
        assert factors == [(1, date(2024, 10, 25), pytest.approx(2.0))]

    def test_implausible_ratio_is_an_error(self):
        """A 5000x discrepancy is a data error, not a corporate action."""
        rows = [(1, 100000.0, 20.0, 100, 100)]
        findings, factors = cross_source_findings(rows, date(2026, 9, 18))
        assert has_errors(findings)
        assert factors == []

    def test_volume_mismatch_warns_but_does_not_halt(self):
        rows = [(1, 200.0, 100.0, 1000, 1000)]
        findings, factors = cross_source_findings(rows, date(2026, 9, 18))
        assert not has_errors(findings)
        assert any(f.check_name == "volume_factor_mismatch" for f in findings)
        assert len(factors) == 1

    def test_empty_input_is_clean(self):
        assert cross_source_findings([], date(2026, 9, 18)) == ([], [])


class TestDuplicateDetection:
    def test_detects_duplicate_symbol_date(self):
        dupes = find_duplicates([bar("RELIANCE"), bar("RELIANCE"), bar("TCS")])
        assert dupes == [("RELIANCE", date(2026, 9, 18), "EQ")]

    def test_no_duplicates_in_clean_batch(self):
        assert find_duplicates([bar("A"), bar("B")]) == []

    def test_same_symbol_different_series_is_not_duplicate(self):
        a = bar("X")
        b = Bar("X", date(2026, 9, 18), 100, 110, 95, 105, 10, series="BE")
        assert find_duplicates([a, b]) == []

    def test_duplicate_batch_is_rejected(self):
        findings = validate_batch([bar("RELIANCE"), bar("RELIANCE")], date(2026, 9, 18))
        assert has_errors(findings)
        assert any(f.check_name == "duplicate_bar" for f in findings)


class TestOhlcSanity:
    def test_clean_bar_has_no_problems(self):
        assert check_ohlc_sane(bar()) == []

    def test_high_below_low(self):
        assert check_ohlc_sane(bar(h=90.0, lo=95.0))

    def test_high_below_close(self):
        assert check_ohlc_sane(bar(h=100.0, c=105.0))

    def test_low_above_open(self):
        assert check_ohlc_sane(bar(lo=101.0, o=100.0))

    def test_negative_volume(self):
        assert check_ohlc_sane(bar(v=-1))

    def test_zero_price(self):
        assert check_ohlc_sane(bar(o=0.0))

    def test_insane_bar_rejects_the_batch(self):
        findings = validate_batch([bar(h=1.0, lo=99.0)], date(2026, 9, 18))
        assert has_errors(findings)


class TestMissingData:
    def test_empty_batch_is_an_error(self):
        findings = validate_batch([], date(2026, 9, 18))
        assert has_errors(findings)
        assert findings[0].check_name == "empty_batch"

    def test_date_mismatch_is_an_error(self):
        """A bar dated differently from the file would silently corrupt history."""
        findings = validate_batch([bar(d=date(2026, 9, 17))], date(2026, 9, 18))
        assert has_errors(findings)
        assert any(f.check_name == "date_mismatch" for f in findings)

    def test_missing_dates_are_reported_in_order(self):
        expected = {date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)}
        settled = {date(2026, 9, 15)}
        assert missing_trading_dates(expected, settled) == [
            date(2026, 9, 14), date(2026, 9, 16),
        ]

    def test_nothing_missing_when_all_settled(self):
        days = {date(2026, 9, 14), date(2026, 9, 15)}
        assert missing_trading_dates(days, days) == []


class TestCandidateDates:
    def test_excludes_weekends(self):
        # 2026-09-19 is a Saturday, 2026-09-20 a Sunday.
        days = candidate_dates(date(2026, 9, 18), date(2026, 9, 21))
        assert days == [date(2026, 9, 18), date(2026, 9, 21)]

    def test_is_ordered_oldest_first(self):
        days = candidate_dates(date(2026, 9, 1), date(2026, 9, 30))
        assert days == sorted(days)

    def test_single_weekend_day_range_is_empty(self):
        assert candidate_dates(date(2026, 9, 19), date(2026, 9, 20)) == []


class TestSeverity:
    def test_has_errors_true_only_for_error_severity(self):
        assert has_errors([Finding("c", "error", "d")])
        assert not has_errors([Finding("c", "warning", "d"), Finding("c", "info", "d")])
        assert not has_errors([])
