#!/usr/bin/env python3
"""Chunked, resumable historical backfill.

Kept separate from the daily job on purpose. A full adjusted backfill is
roughly 13,900 Upstox requests (~3.5 h at the documented 2,000 req/30 min),
which would sit uncomfortably inside the 6-hour GitHub Actions job limit.
Run it in batches with --limit and re-run until it reports nothing left.

    python3 scripts/backfill.py --source nse    --start 2024-01-01 --end 2024-12-31
    python3 scripts/backfill.py --source upstox --start 2016-01-01 --limit 50

Read-only with respect to the market.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import integrity  # noqa: E402
from src.config import load_settings  # noqa: E402
from src.db.repository import Repository, apply_schema, connect  # noqa: E402
from src.pipeline import NSE_SOURCE  # noqa: E402
from src.sources.nse import NseSource  # noqa: E402
from src.sources.upstox import UpstoxSource  # noqa: E402

log = logging.getLogger("backfill")


def backfill_nse(repo: Repository, start: date, end: date, limit: int, run_id: int) -> int:
    nse = NseSource()
    settled = repo.settled_dates(NSE_SOURCE)
    pending = [d for d in integrity.candidate_dates(start, end) if d not in settled]
    if limit:
        pending = pending[:limit]
    log.info("%d date(s) outstanding in range", len(pending))

    ticker_ids = repo.symbol_ids_by_ticker()
    if not ticker_ids:
        log.info("symbol table empty - loading universe first")
        rows = nse.fetch_universe()
        repo.upsert_symbols(rows)
        ticker_ids = repo.symbol_ids_by_ticker()

    loaded = 0
    for trade_date in pending:
        try:
            bars = nse.fetch_daily_bars(trade_date)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s failed: %s", trade_date, exc)
            repo.mark_ingestion(NSE_SOURCE, trade_date, "failed", 0, run_id)
            continue
        if bars is None:
            repo.mark_ingestion(NSE_SOURCE, trade_date, "no_data", 0, run_id)
            continue
        findings = integrity.validate_batch(bars, trade_date)
        if integrity.has_errors(findings):
            log.error("%s rejected: %s", trade_date, findings[0].detail)
            repo.mark_ingestion(NSE_SOURCE, trade_date, "failed", 0, run_id)
            continue
        written = repo.upsert_raw_bars(bars, ticker_ids, nse.name)
        repo.mark_ingestion(NSE_SOURCE, trade_date, "loaded", written, run_id)
        loaded += written
        log.info("%s: %d bars", trade_date, written)
    return loaded


def backfill_upstox(
    repo: Repository, token: str, start: date, end: date, limit: int
) -> int:
    source = UpstoxSource(token)
    with repo.conn.cursor() as cur:
        cur.execute("SELECT isin, symbol_id FROM symbols ORDER BY symbol")
        isin_map = {row[0]: row[1] for row in cur.fetchall()}
    if not isin_map:
        log.error("symbol table is empty - run daily_update.py first")
        return 0

    isins = list(isin_map)[:limit] if limit else list(isin_map)
    log.info("fetching adjusted history for %d instrument(s)", len(isins))

    written = 0
    for index, isin in enumerate(isins, 1):
        bars = source.fetch_daily(isin, start, end)
        if bars:
            written += repo.upsert_adjusted_bars(bars, isin_map, source.name)
        if index % 25 == 0:
            log.info("  %d/%d instruments, %d bars written", index, len(isins), written)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("nse", "upstox"), required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--limit", type=int, default=0, help="cap the batch size; 0 = no cap")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    settings = load_settings()
    if not settings.has_database:
        log.error("DATABASE_URL is not set")
        return 2

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = (
        datetime.strptime(args.end, "%Y-%m-%d").date()
        if args.end
        else date.today() - timedelta(days=1)
    )

    with connect(settings.database_url) as conn:
        apply_schema(conn)
        repo = Repository(conn)
        run_id = repo.start_run(settings.commit_sha)
        try:
            if args.source == "nse":
                total = backfill_nse(repo, start, end, args.limit, run_id)
            else:
                if not settings.has_upstox:
                    log.error("UPSTOX_ANALYTICS_TOKEN is not set")
                    repo.finish_run(run_id, "failed", "missing upstox token")
                    return 2
                total = backfill_upstox(
                    repo, settings.upstox_analytics_token, start, end, args.limit
                )
            repo.finish_run(run_id, "succeeded", f"backfill wrote {total} rows")
        except Exception as exc:  # noqa: BLE001
            repo.finish_run(run_id, "failed", repr(exc))
            raise
    log.info("backfill complete: %d rows", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
