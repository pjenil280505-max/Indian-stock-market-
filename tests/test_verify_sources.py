"""Offline tests for the Phase 0 verification helpers.

No network access. These cover the pure logic that the live checks depend on,
so a network outage cannot make the scaffolding look broken.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verify_sources import (  # noqa: E402
    adjustment_ratio,
    count_series,
    estimate_pg_bytes,
    parse_candles,
)


class TestAdjustmentRatio:
    def test_detects_reliance_one_for_one_bonus(self):
        """The measured Phase 0 result: exactly 2.0 across all four prices."""
        raw = {"o": 2687.00, "h": 2688.70, "l": 2644.00, "c": 2655.70}
        adjusted = {"o": 1343.50, "h": 1344.35, "l": 1322.00, "c": 1327.85}
        assert adjustment_ratio(raw, adjusted) == 2.0

    def test_unadjusted_series_gives_factor_one(self):
        bar = {"o": 100.0, "h": 110.0, "l": 95.0, "c": 105.0}
        assert adjustment_ratio(bar, bar) == 1.0

    def test_rejects_inconsistent_ratios(self):
        """A factor on some fields but not others is a data error, not a corporate action."""
        raw = {"o": 200.0, "h": 220.0, "l": 190.0, "c": 210.0}
        adjusted = {"o": 100.0, "h": 110.0, "l": 95.0, "c": 150.0}
        assert adjustment_ratio(raw, adjusted) is None

    def test_rejects_zero_price(self):
        raw = {"o": 200.0, "h": 220.0, "l": 190.0, "c": 210.0}
        adjusted = {"o": 0.0, "h": 110.0, "l": 95.0, "c": 105.0}
        assert adjustment_ratio(raw, adjusted) is None

    def test_rejects_missing_field(self):
        assert adjustment_ratio({"o": 1.0}, {"o": 1.0}) is None

    def test_handles_five_for_one_split(self):
        raw = {"o": 500.0, "h": 550.0, "l": 475.0, "c": 525.0}
        adjusted = {"o": 100.0, "h": 110.0, "l": 95.0, "c": 105.0}
        assert adjustment_ratio(raw, adjusted) == pytest.approx(5.0)


class TestCountSeries:
    def test_counts_by_series_with_leading_space_headers(self):
        """NSE's real header is 'SYMBOL,NAME OF COMPANY, SERIES, ...' - note the spaces."""
        csv_bytes = (
            b"SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, ISIN NUMBER\n"
            b"AAA,Alpha Ltd,EQ,01-JAN-2000,INE001A01001\n"
            b"BBB,Beta Ltd,EQ,01-JAN-2001,INE002A01002\n"
            b"CCC,Gamma Ltd,BE,01-JAN-2002,INE003A01003\n"
        )
        assert count_series(csv_bytes) == {"EQ": 2, "BE": 1}

    def test_empty_body_yields_no_counts(self):
        assert count_series(b"") == {}


class TestParseCandles:
    def test_parses_upstox_shape(self):
        body = (
            b'{"status":"success","data":{"candles":'
            b'[["2026-09-18T00:00:00+05:30",1245.0,1247.3,1226.4,1226.4,15122715,0]]}}'
        )
        candles = parse_candles(body)
        assert len(candles) == 1
        assert candles[0][4] == 1226.4

    def test_error_response_yields_empty_list(self):
        body = b'{"status":"error","errors":[{"errorCode":"UDAPI1148"}]}'
        assert parse_candles(body) == []

    def test_malformed_body_yields_empty_list(self):
        assert parse_candles(b"not json") == []


class TestStorageEstimate:
    def test_ten_years_liquid_universe_fits_free_tier(self):
        assert estimate_pg_bytes(1200, 10) / 1e9 < 0.5

    def test_full_universe_full_history_exceeds_free_tier(self):
        """2317 EQ symbols x 26 years does not fit 0.5 GB - drives the Phase 1 scope."""
        assert estimate_pg_bytes(2317, 26) / 1e9 > 0.5

    def test_scales_linearly(self):
        assert estimate_pg_bytes(100, 2) == 2 * estimate_pg_bytes(100, 1)
