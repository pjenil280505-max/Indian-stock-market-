#!/usr/bin/env python3
"""Re-run the Phase 0 data-source checks.

Read-only. Makes a small number of HTTPS requests to public endpoints, identifies
itself with an honest User-Agent, and prints a pass/fail table.

Usage:
    python3 scripts/verify_sources.py [--offline]

Findings this reproduces are documented in docs/PHASE_0_REPORT.md section 0.4.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Identify the client honestly. Upstox rejects the default "Python-urllib/x.y"
# User-Agent with HTTP 403 (Phase 0 check 13); spoofing a browser string would
# work but is the wrong posture toward a data provider.
USER_AGENT = "indian-stock-research/0.1 (Phase 0 source verification)"
TIMEOUT = 30

NSE_ARCHIVES = "https://nsearchives.nseindia.com"
UPSTOX_HIST = "https://api.upstox.com/v3/historical-candle"
RELIANCE = "NSE_EQ%7CINE002A01018"  # NSE_EQ|INE002A01018


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.results.append(Result(name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)


def fetch(url: str) -> tuple[int, bytes]:
    """GET a URL. Returns (status, body); status 0 on transport error."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except Exception:
        return 0, b""


# --- pure helpers, unit-tested offline -------------------------------------


def count_series(csv_bytes: bytes) -> dict[str, int]:
    """Count NSE EQUITY_L.csv rows by SERIES. Header fields carry leading spaces."""
    rows = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8", "replace")))
    counts: dict[str, int] = {}
    for row in rows:
        key = next((k for k in row if k.strip() == "SERIES"), None)
        series = (row.get(key) or "").strip() if key else ""
        counts[series] = counts.get(series, 0) + 1
    return counts


def parse_candles(body: bytes) -> list[list]:
    """Extract the candle list from an Upstox v3 historical response."""
    try:
        return json.loads(body)["data"]["candles"]
    except Exception:
        return []


def adjustment_ratio(raw: dict[str, float], adjusted: dict[str, float]) -> float | None:
    """Price adjustment factor between a raw and an adjusted OHLC bar.

    Returns the factor only if every price field agrees on it, else None.
    A single consistent factor across all four prices is what distinguishes a
    corporate-action adjustment from a data error.
    """
    ratios = []
    for field_name in ("o", "h", "l", "c"):
        if field_name not in raw or field_name not in adjusted:
            return None
        if not adjusted[field_name]:
            return None
        ratios.append(raw[field_name] / adjusted[field_name])
    if max(ratios) - min(ratios) > 1e-6:
        return None
    return ratios[0]


def estimate_pg_bytes(symbols: int, years: int, days_per_year: int = 250) -> int:
    """Rough Postgres size for a daily OHLCV table.

    56 B payload (date + 4 float8 prices + int8 volume + fk) plus ~40 B of row
    overhead and index. Used for free-tier capacity planning, not billing.
    """
    return symbols * years * days_per_year * (56 + 40)


# --- live checks -----------------------------------------------------------


def check_nse_universe(rep: Report) -> None:
    status, body = fetch(f"{NSE_ARCHIVES}/content/equities/EQUITY_L.csv")
    if status != 200 or not body:
        rep.add("NSE EQUITY_L.csv", False, f"HTTP {status}")
        return
    counts = count_series(body)
    eq = counts.get("EQ", 0)
    rep.add(
        "NSE EQUITY_L.csv",
        eq > 1500,
        f"HTTP 200, {len(body)} bytes, EQ={eq} BE={counts.get('BE', 0)} BZ={counts.get('BZ', 0)}",
    )


def check_nse_bhavcopy(rep: Report, date_yyyymmdd: str) -> None:
    url = f"{NSE_ARCHIVES}/content/cm/BhavCopy_NSE_CM_0_0_0_{date_yyyymmdd}_F_0000.csv.zip"
    status, body = fetch(url)
    rep.add(
        f"NSE bhavcopy {date_yyyymmdd}",
        status == 200 and len(body) > 10_000,
        f"HTTP {status}, {len(body)} bytes",
    )


def check_nse_delivery(rep: Report, date_ddmmyyyy: str) -> None:
    url = f"{NSE_ARCHIVES}/products/content/sec_bhavdata_full_{date_ddmmyyyy}.csv"
    status, body = fetch(url)
    has_deliv = b"DELIV_PER" in body
    rep.add(
        f"NSE delivery data {date_ddmmyyyy}",
        status == 200 and has_deliv,
        f"HTTP {status}, {len(body)} bytes, DELIV_PER present={has_deliv}",
    )


def check_upstox_daily(rep: Report) -> None:
    url = f"{UPSTOX_HIST}/{RELIANCE}/days/1/2026-09-19/2026-09-01"
    status, body = fetch(url)
    candles = parse_candles(body)
    rep.add(
        "Upstox v3 daily candles",
        status == 200 and len(candles) > 5,
        f"HTTP {status}, {len(candles)} candles",
    )


def check_upstox_history_depth(rep: Report) -> None:
    url = f"{UPSTOX_HIST}/{RELIANCE}/days/1/2001-12-31/2000-01-01"
    status, body = fetch(url)
    candles = parse_candles(body)
    oldest = candles[-1][0][:10] if candles else "n/a"
    rep.add(
        "Upstox history depth (year 2000)",
        status == 200 and len(candles) > 100,
        f"HTTP {status}, {len(candles)} candles, oldest={oldest}",
    )


def check_upstox_window_cap(rep: Report) -> None:
    """A >5y window for days/1 is rejected (UDAPI1148). Bounds backfill chunking."""
    status_5y, body_5y = fetch(f"{UPSTOX_HIST}/{RELIANCE}/days/1/2026-09-19/2021-01-01")
    status_10y, _ = fetch(f"{UPSTOX_HIST}/{RELIANCE}/days/1/2026-09-19/2016-01-01")
    n5 = len(parse_candles(body_5y))
    rep.add(
        "Upstox per-request window cap",
        status_5y == 200 and n5 > 1000 and status_10y != 200,
        f"5y -> HTTP {status_5y} ({n5} candles), 10y -> HTTP {status_10y} (expected non-200)",
    )


def check_corporate_action_adjustment(rep: Report) -> None:
    """Reliance 1:1 bonus, ex-date 28-Oct-2024.

    Upstox back-adjusts; NSE bhavcopy is raw. Confirms the two sources answer
    different questions (report section 2.4).
    """
    status, body = fetch(f"{UPSTOX_HIST}/{RELIANCE}/days/1/2024-10-25/2024-10-25")
    candles = parse_candles(body)
    if status != 200 or not candles:
        rep.add("Corporate-action adjustment", False, f"HTTP {status}, no candle")
        return
    _, o, h, l, c, *_ = candles[0]
    # NSE raw bhavcopy values for RELIANCE on 25-Oct-2024 (pre-ex), from
    # sec_bhavdata_full_25102024.csv.
    raw = {"o": 2687.00, "h": 2688.70, "l": 2644.00, "c": 2655.70}
    factor = adjustment_ratio(raw, {"o": o, "h": h, "l": l, "c": c})
    ok = factor is not None and abs(factor - 2.0) < 1e-4
    rep.add(
        "Corporate-action adjustment (1:1 bonus)",
        ok,
        f"factor={factor!r} vs NSE raw (expect 2.0 on all four prices)",
    )


def check_telegram_reachable(rep: Report) -> None:
    """401 to a deliberately invalid token proves the network path without a secret."""
    status, _ = fetch("https://api.telegram.org/bot000:INVALID/getMe")
    rep.add(
        "api.telegram.org reachable",
        status in (401, 404),
        f"HTTP {status} to an invalid token (401/404 = correct rejection)",
    )


def run_offline_checks(rep: Report) -> None:
    factor = adjustment_ratio(
        {"o": 2687.00, "h": 2688.70, "l": 2644.00, "c": 2655.70},
        {"o": 1343.50, "h": 1344.35, "l": 1322.00, "c": 1327.85},
    )
    rep.add("adjustment_ratio arithmetic", factor == 2.0, f"factor={factor}")
    gb = estimate_pg_bytes(1200, 10) / 1e9
    rep.add("storage estimate 10y x 1200 symbols", gb < 0.5, f"{gb:.2f} GB (Neon free tier 0.5 GB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="skip network checks")
    args = parser.parse_args()

    rep = Report()
    print("Phase 0 source verification\n")
    print("Offline logic checks:")
    run_offline_checks(rep)

    if not args.offline:
        print("\nLive source checks:")
        check_nse_universe(rep)
        check_nse_bhavcopy(rep, "20260921")
        check_nse_delivery(rep, "18092026")
        check_upstox_daily(rep)
        check_upstox_history_depth(rep)
        check_upstox_window_cap(rep)
        check_corporate_action_adjustment(rep)
        check_telegram_reachable(rep)

    total = len(rep.results)
    print(f"\n{total - rep.failed}/{total} checks passed")
    if rep.failed:
        print("A failure here may mean a source changed. See docs/PHASE_0_REPORT.md section 2.6.")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
