#!/usr/bin/env python3
"""Independent data-freshness check (Phase 1.1). READ-ONLY.

    python3 scripts/freshness_check.py [--lookback-days 7]

Answers: is every recent trading day that NSE has published actually loaded
in Neon? It does not depend on Cloudflare or on the dispatch token, so it
still works when the primary collector is broken - that is its purpose.

For each weekday in the lookback window before today (IST):
  loaded / partial      -> ok
  no_data               -> ok (a learned market holiday)
  failed / no row       -> ask NSE: published? -> STALE; 404 -> not a
                           trading day (or not yet published) -> ok

Opens a server-enforced read-only database connection; never writes.
Prints public facts only, never the connection string.

Exit codes: 0 fresh, 2 DATABASE_URL missing, 7 stale (lines start "STALE"),
            8 could not verify (NSE unreachable for an unloaded date).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_settings  # noqa: E402
from src.db.repository import connect  # noqa: E402
from src.pipeline import NSE_SOURCE  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
OK_STATUSES = {"loaded", "partial", "no_data"}


def expected_dates(today: date, lookback_days: int) -> list[date]:
    """Weekdays strictly before `today`, newest first. Pure."""
    days = (today - timedelta(days=n) for n in range(1, lookback_days + 1))
    return [d for d in days if d.weekday() < 5]


def assess(dates: list[date], statuses: dict[date, str], is_published) -> list[tuple[date, str, str]]:
    """(date, verdict, note) per date. verdict in ok|STALE|UNVERIFIED.

    `is_published(d)` returns True/False, or raises if NSE cannot be reached.
    It is only called for dates that are not already accounted for.
    """
    out = []
    for d in dates:
        status = statuses.get(d)
        if status in OK_STATUSES:
            out.append((d, "ok", status))
            continue
        try:
            published = is_published(d)
        except Exception as exc:  # noqa: BLE001 - report, never guess
            out.append((d, "UNVERIFIED", f"status={status or 'none'}; NSE unreachable ({type(exc).__name__})"))
            continue
        if published:
            out.append((d, "STALE", f"published by NSE but status={status or 'none'} in ingestion_log"))
        else:
            out.append((d, "ok", f"status={status or 'none'}; not published (holiday)"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=7)
    args = parser.parse_args()

    settings = load_settings()
    if not settings.has_database:
        print("FAIL: DATABASE_URL is not set")
        return 2

    today = datetime.now(IST).date()
    dates = expected_dates(today, args.lookback_days)

    conn = connect(settings.database_url, read_only=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data_date, status FROM ingestion_log WHERE source = %s AND data_date >= %s",
                (NSE_SOURCE, min(dates) if dates else today),
            )
            statuses = dict(cur.fetchall())
            cur.execute("SELECT max(trade_date), count(*) FROM daily_bars_raw")
            latest, rows = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()

    from src.sources.nse import NseSource  # imported late: offline tests never need it

    nse = NseSource()
    results = assess(dates, statuses, lambda d: nse.fetch_daily_bars(d) is not None)

    print(f"today (IST): {today}; latest bar in Neon: {latest}; daily_bars_raw rows: {rows:,}")
    for d, verdict, note in results:
        print(f"{verdict} {d} {note}")

    if any(v == "STALE" for _, v, _ in results):
        return 7
    if any(v == "UNVERIFIED" for _, v, _ in results):
        return 8
    print("OK: every published trading day in the window is loaded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
