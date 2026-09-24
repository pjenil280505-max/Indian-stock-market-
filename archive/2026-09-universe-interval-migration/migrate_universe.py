#!/usr/bin/env python3
"""Migrate universe_snapshots (one row per symbol per day) to intervals.

The old table recorded ~2,583 rows every day to capture membership that
changes a few times a month - roughly 63 MB/year. universe_membership stores
one row per continuous listing span instead.

This migration is deliberately cautious, because universe membership is the
survivorship-bias control and cannot be reconstructed if it is lost:

  1. Build intervals from the snapshots, splitting on any gap in observation
     dates so a delist-and-relist becomes two intervals rather than one.
  2. VERIFY equivalence - for every observed date, the set of symbols returned
     by the interval table must exactly equal the snapshot rows.
  3. Only if verification passes, and only with --drop-old, remove the old
     table.

    python3 scripts/migrate_universe.py                # migrate + verify
    python3 scripts/migrate_universe.py --drop-old     # ...then drop old table
    python3 scripts/migrate_universe.py --verify-only  # check without writing

Exit codes: 0 success, 2 DATABASE_URL missing, 3 verification failed.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_settings  # noqa: E402
from src.db.repository import apply_schema, connect  # noqa: E402

log = logging.getLogger("migrate_universe")


def table_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (name,))
        return cur.fetchone()[0] is not None


def observed_dates(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT snapshot_date FROM universe_snapshots ORDER BY 1")
        return [row[0] for row in cur.fetchall()]


def build_intervals(conn) -> int:
    """Collapse snapshot rows into intervals.

    Uses the classic gaps-and-islands technique: rank each symbol's observation
    dates against the global sequence of observation dates, and start a new
    interval wherever that alignment breaks.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO universe_membership
                (symbol_id, series, valid_from, valid_to, listing_date)
            WITH all_dates AS (
                SELECT snapshot_date,
                       row_number() OVER (ORDER BY snapshot_date) AS global_rn
                  FROM (SELECT DISTINCT snapshot_date FROM universe_snapshots) d
            ),
            ranked AS (
                SELECT u.symbol_id,
                       u.series,
                       u.listing_date,
                       u.snapshot_date,
                       a.global_rn,
                       row_number() OVER (
                           PARTITION BY u.symbol_id, u.series ORDER BY u.snapshot_date
                       ) AS symbol_rn
                  FROM universe_snapshots u
                  JOIN all_dates a USING (snapshot_date)
            )
            SELECT symbol_id,
                   series,
                   min(snapshot_date) AS valid_from,
                   max(snapshot_date) AS valid_to,
                   min(listing_date)  AS listing_date
              FROM ranked
             GROUP BY symbol_id, series, (global_rn - symbol_rn)
            ON CONFLICT (symbol_id, series, valid_from) DO NOTHING
            """
        )
        written = cur.rowcount
    conn.commit()
    return written


def verify(conn) -> bool:
    """Every observed date must yield an identical symbol set from both tables."""
    dates = observed_dates(conn)
    if not dates:
        log.info("no snapshot rows to verify")
        return True

    log.info("verifying %d observed date(s)", len(dates))
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH snap AS (
                SELECT snapshot_date AS d, symbol_id, series FROM universe_snapshots
            ),
            ivl AS (
                SELECT s.d, m.symbol_id, m.series
                  FROM (SELECT DISTINCT snapshot_date AS d FROM universe_snapshots) s
                  JOIN universe_membership m
                    ON m.valid_from <= s.d AND m.valid_to >= s.d
            )
            SELECT
                (SELECT count(*) FROM (SELECT * FROM snap EXCEPT SELECT * FROM ivl) x),
                (SELECT count(*) FROM (SELECT * FROM ivl EXCEPT SELECT * FROM snap) y)
            """
        )
        missing, extra = cur.fetchone()

    if missing or extra:
        log.error(
            "VERIFICATION FAILED: %d row(s) in snapshots but not intervals, "
            "%d row(s) in intervals but not snapshots", missing, extra,
        )
        return False
    log.info("verification PASSED: interval table reproduces every snapshot exactly")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drop-old", action="store_true",
                        help="drop universe_snapshots after verification passes")
    parser.add_argument("--verify-only", action="store_true",
                        help="verify without writing anything")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    settings = load_settings()
    if not settings.has_database:
        log.error("DATABASE_URL is not set")
        return 2

    with connect(settings.database_url) as conn:
        apply_schema(conn)  # ensures universe_membership exists

        if not table_exists(conn, "universe_snapshots"):
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM universe_membership")
                total = cur.fetchone()[0]
            log.info("universe_snapshots is already gone; %d interval row(s) present", total)
            return 0

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM universe_snapshots")
            old_rows = cur.fetchone()[0]
            cur.execute("SELECT pg_total_relation_size('universe_snapshots')")
            old_bytes = cur.fetchone()[0]
        log.info("universe_snapshots: %d row(s), %.1f MB", old_rows, old_bytes / 1e6)

        if not args.verify_only:
            written = build_intervals(conn)
            log.info("built %d interval row(s)", written)

        if not verify(conn):
            log.error("refusing to proceed - the old table is left untouched")
            return 3

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM universe_membership")
            new_rows = cur.fetchone()[0]
            cur.execute("SELECT pg_total_relation_size('universe_membership')")
            new_bytes = cur.fetchone()[0]

        log.info("universe_membership: %d row(s), %.2f MB", new_rows, new_bytes / 1e6)
        if old_rows:
            log.info("row reduction: %.1f%%", (1 - new_rows / old_rows) * 100)

        if args.drop_old:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE universe_snapshots")
            conn.commit()
            log.info("dropped universe_snapshots (verification had passed)")
            with conn.cursor() as cur:
                cur.execute("SELECT pg_database_size(current_database())")
                log.info("database size now %.1f MB", cur.fetchone()[0] / 1e6)
        else:
            log.info("old table kept; re-run with --drop-old to remove it")

    return 0


if __name__ == "__main__":
    sys.exit(main())
