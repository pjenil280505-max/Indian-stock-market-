#!/usr/bin/env python3
"""Daily data-update entry point.

Read-only with respect to the market: fetches published data, writes to our
own database, places no orders and contacts no broker for execution.

    python3 scripts/daily_update.py [--date YYYY-MM-DD] [--catchup-days N] [--dry-run]

Requires DATABASE_URL. UPSTOX_ANALYTICS_TOKEN is optional in Phase 1.
Exits non-zero if the run halted on an integrity error, so CI surfaces it.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_settings  # noqa: E402
from src.db.repository import Repository, apply_schema, connect  # noqa: E402
from src.pipeline import DEFAULT_CATCHUP_DAYS, DailyPipeline  # noqa: E402
from src.sources.nse import NseSource  # noqa: E402
from src.sources.upstox import UpstoxSource  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="treat this as today (YYYY-MM-DD)")
    parser.add_argument("--catchup-days", type=int, default=DEFAULT_CATCHUP_DAYS)
    parser.add_argument("--dry-run", action="store_true", help="fetch only, do not write")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("daily_update")

    today = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    )
    settings = load_settings()

    if args.dry_run:
        nse = NseSource()
        universe = nse.fetch_universe()
        log.info("dry run: fetched %d universe rows", len(universe))
        bars = nse.fetch_daily_bars(today)
        log.info("dry run: %s bars for %s", "no" if bars is None else len(bars), today)
        return 0

    if not settings.has_database:
        log.error("DATABASE_URL is not set; nothing to write to")
        return 2

    upstox = (
        UpstoxSource(settings.upstox_analytics_token) if settings.has_upstox else None
    )
    if upstox is None:
        log.warning("UPSTOX_ANALYTICS_TOKEN not set - adjusted bars will be skipped")

    with connect(settings.database_url) as conn:
        apply_schema(conn)
        repo = Repository(conn)
        pipeline = DailyPipeline(repo, nse=NseSource(), upstox=upstox)
        summary = pipeline.run(today=today, catchup_days=args.catchup_days)

    print(summary.as_text())
    for finding in summary.error_findings[:20]:
        log.error("%s: %s", finding.check_name, finding.detail)

    if summary.halted:
        log.error("run HALTED on integrity errors - data was not accepted")
        return 1
    if summary.dates_failed:
        log.warning("some dates failed and will be retried next run: %s", summary.dates_failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
