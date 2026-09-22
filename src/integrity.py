"""Data-integrity checks.

The pipeline HALTS on an error-severity finding. A wrong report is worse than
no report (docs/PHASE_0_REPORT.md section 9, step 4).

The central check is the cross-source adjustment comparison from report
section 2.4: NSE raw and Upstox adjusted should differ by exactly one
consistent factor per symbol per day. Anything else is a data-quality alarm.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# A corporate action changes the factor by a clean ratio (2.0 for a 1:1 bonus,
# 5.0 for a 1:5 split). Tiny deviations are rounding in the published files.
FACTOR_TOLERANCE = 0.005
# Beyond this, an apparent "adjustment" is more likely a data error.
MAX_PLAUSIBLE_FACTOR = 1000.0


@dataclass(frozen=True)
class Finding:
    check_name: str
    severity: str  # 'info' | 'warning' | 'error'
    detail: str
    symbol_id: int | None = None
    trade_date: date | None = None


def price_adjustment_factor(raw_close: float, adj_close: float) -> float | None:
    """raw / adjusted. None if either side is unusable."""
    if not raw_close or not adj_close or raw_close <= 0 or adj_close <= 0:
        return None
    return float(raw_close) / float(adj_close)


def check_volume_adjustment_consistency(
    raw_close: float, adj_close: float, raw_volume: int, adj_volume: int
) -> tuple[float | None, bool]:
    """Verify volume moves inversely to price under a corporate action.

    Phase 0 proved Upstox scales prices DOWN and volume UP by the same factor:
    Reliance 1:1 bonus gave price factor 2.0 and volume factor 0.5 exactly.

    Returns (price_factor, is_consistent). The direct consequence is that
    adjusted volume is inflated by past corporate actions, so liquidity
    filters must use turnover or raw volume - never adjusted volume.
    """
    factor = price_adjustment_factor(raw_close, adj_close)
    if factor is None:
        return None, False
    if not raw_volume or not adj_volume:
        return factor, False
    volume_factor = float(raw_volume) / float(adj_volume)
    expected = 1.0 / factor
    return factor, abs(volume_factor - expected) <= FACTOR_TOLERANCE * max(expected, 1.0)


def check_ohlc_sane(bar) -> list[str]:
    """Structural checks on a single bar. Returns a list of problems."""
    problems = []
    if bar.high < bar.low:
        problems.append(f"high {bar.high} < low {bar.low}")
    if bar.high < bar.open or bar.high < bar.close:
        problems.append(f"high {bar.high} below open/close")
    if bar.low > bar.open or bar.low > bar.close:
        problems.append(f"low {bar.low} above open/close")
    if bar.open <= 0 or bar.close <= 0:
        problems.append("non-positive price")
    if bar.volume < 0:
        problems.append(f"negative volume {bar.volume}")
    return problems


def find_duplicates(bars) -> list[tuple]:
    """Duplicate (symbol, trade_date, series) keys within one batch.

    The database primary key would reject these, but catching them here names
    the offending symbol instead of surfacing an opaque constraint violation.
    """
    seen: dict[tuple, int] = {}
    for bar in bars:
        key = (bar.symbol, bar.trade_date, getattr(bar, "series", ""))
        seen[key] = seen.get(key, 0) + 1
    return sorted(key for key, count in seen.items() if count > 1)


def missing_trading_dates(expected: set[date], settled: set[date]) -> list[date]:
    """Dates that should have been loaded but have not settled. Drives catch-up."""
    return sorted(expected - settled)


def candidate_dates(start: date, end: date) -> list[date]:
    """Weekdays in a range, oldest first.

    Deliberately NOT a holiday calendar. A date is confirmed non-trading when
    the archive returns 404, which is recorded as 'no_data'. This is
    self-correcting and needs no holiday list to maintain.
    """
    days = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:  # Mon-Fri
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def validate_batch(bars, trade_date: date) -> list[Finding]:
    """Structural validation of one day's bars before they reach the database."""
    findings: list[Finding] = []

    if not bars:
        return [Finding("empty_batch", "error", f"no bars parsed for {trade_date}", None, trade_date)]

    for key in find_duplicates(bars):
        findings.append(
            Finding("duplicate_bar", "error", f"duplicate key {key}", None, trade_date)
        )

    for bar in bars:
        for problem in check_ohlc_sane(bar):
            findings.append(
                Finding("ohlc_insane", "error", f"{bar.symbol}: {problem}", None, trade_date)
            )
        if bar.trade_date != trade_date:
            findings.append(
                Finding(
                    "date_mismatch", "error",
                    f"{bar.symbol}: bar dated {bar.trade_date}, expected {trade_date}",
                    None, trade_date,
                )
            )
    return findings


def cross_source_findings(paired_rows, trade_date: date) -> tuple[list[Finding], list[tuple]]:
    """Compare raw and adjusted series for one date.

    Returns (findings, adjustment_factor_rows) where each factor row is
    (symbol_id, trade_date, factor) ready for persistence.
    """
    findings: list[Finding] = []
    factors: list[tuple] = []

    for symbol_id, raw_close, adj_close, raw_volume, adj_volume in paired_rows:
        factor, volume_ok = check_volume_adjustment_consistency(
            float(raw_close), float(adj_close), raw_volume, adj_volume
        )
        if factor is None:
            findings.append(
                Finding("factor_uncomputable", "warning",
                        "raw or adjusted close unusable", symbol_id, trade_date)
            )
            continue

        if factor > MAX_PLAUSIBLE_FACTOR or factor < 1.0 / MAX_PLAUSIBLE_FACTOR:
            findings.append(
                Finding("implausible_factor", "error",
                        f"raw/adjusted close ratio {factor:.6f} is implausible",
                        symbol_id, trade_date)
            )
            continue

        if not volume_ok:
            # Warning, not error: Upstox and NSE occasionally differ slightly
            # on reported volume even absent a corporate action.
            findings.append(
                Finding("volume_factor_mismatch", "warning",
                        f"price factor {factor:.6f} but volume does not scale inversely",
                        symbol_id, trade_date)
            )
        factors.append((symbol_id, trade_date, round(factor, 8)))

    return findings, factors


def has_errors(findings) -> bool:
    return any(f.severity == "error" for f in findings)
