#!/usr/bin/env python3
"""Explain changes in universe membership between two observations. READ-ONLY.

    python3 scripts/universe_report.py [--since YYYY-MM-DD]

Opens a server-enforced read-only connection (it cannot write even by
mistake) and reports, for every interval that opened on or after --since
(default: the most recent observation date):

  new_symbol     first interval ever for this ISIN - a new listing
  series_change  the symbol's previous interval had a different series
  gap_relist     same series, but absent for at least one observation
  SUSPECT_SPLIT  same series and the previous interval ended at the
                 previous observation - it should have been EXTENDED, so
                 this is the survivorship-bias bug the interval rules exist
                 to prevent

and every interval that closed at the previous observation (present then,
absent now). Prints public market identifiers only; never the connection
string.

Exit codes: 0 ok, 2 DATABASE_URL missing, 6 SUSPECT_SPLIT found.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_settings  # noqa: E402
from src.db.repository import connect  # noqa: E402

OBSERVATIONS_SQL = """
    SELECT DISTINCT valid_to FROM universe_membership ORDER BY valid_to DESC LIMIT 2
"""

# Every interval opened on/after :since, with the symbol's immediately
# preceding interval (if any).
OPENED_SQL = """
    SELECT s.symbol, s.isin, s.first_seen, m.series, m.valid_from, m.valid_to,
           p.series AS prev_series, p.valid_to AS prev_valid_to
      FROM universe_membership m
      JOIN symbols s USING (symbol_id)
      LEFT JOIN LATERAL (
            SELECT series, valid_to FROM universe_membership q
             WHERE q.symbol_id = m.symbol_id AND q.valid_from < m.valid_from
             ORDER BY q.valid_from DESC LIMIT 1
      ) p ON true
     WHERE m.valid_from >= %s
     ORDER BY s.symbol, m.series
"""

# Present at the previous observation, not extended to the latest one.
CLOSED_SQL = """
    SELECT s.symbol, s.isin, m.series, m.valid_from, m.valid_to
      FROM universe_membership m
      JOIN symbols s USING (symbol_id)
     WHERE m.valid_to = %s
     ORDER BY s.symbol, m.series
"""

SIZE_SQL = "SELECT count(*) FROM universe_membership WHERE valid_from <= %s AND valid_to >= %s"


def classify(prev_series, prev_valid_to, series, previous_observation) -> str:
    if prev_series is None:
        return "new_symbol"
    if prev_series != series:
        return "series_change"
    if previous_observation is not None and prev_valid_to == previous_observation:
        return "SUSPECT_SPLIT"
    return "gap_relist"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="report intervals opened on/after this date (YYYY-MM-DD)")
    parser.add_argument("--previous", help="the previous observation date (YYYY-MM-DD); inferred if omitted")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.has_database:
        print("FAIL: DATABASE_URL is not set")
        return 2

    conn = connect(settings.database_url, read_only=True)
    try:
        with conn.cursor() as cur:
            cur.execute(OBSERVATIONS_SQL)
            obs = [r[0] for r in cur.fetchall()]
            latest = obs[0] if obs else None
            # Inferred as the second-latest valid_to, which is only right if some
            # interval closed then; pass --previous when the date is known.
            previous = (datetime.strptime(args.previous, "%Y-%m-%d").date() if args.previous
                        else (obs[1] if len(obs) > 1 else None))
            since = datetime.strptime(args.since, "%Y-%m-%d").date() if args.since else latest

            print(f"latest observation:   {latest}")
            print(f"previous observation: {previous}")
            for d in (previous, latest):
                if d:
                    cur.execute(SIZE_SQL, (d, d))
                    print(f"universe size on {d}: {cur.fetchone()[0]}")

            cur.execute(OPENED_SQL, (since,))
            opened = cur.fetchall()
            cur.execute(CLOSED_SQL, (previous,))
            closed = cur.fetchall() if previous else []
        conn.rollback()
    finally:
        conn.close()

    kinds = Counter()
    print(f"\nintervals opened on/after {since}: {len(opened)}")
    print(f"  {'symbol':14s} {'series':6s} {'valid_from':10s} {'first_seen':10s} {'prev':6s} {'prev_to':10s} kind")
    for symbol, isin, first_seen, series, vfrom, _vto, pseries, pto in opened:
        kind = classify(pseries, pto, series, previous)
        kinds[kind] += 1
        print(f"  {symbol:14s} {series:6s} {vfrom!s:10s} {first_seen!s:10s} {pseries or '-':6s} {pto or '-'!s:10s} {kind}")
    print("  by kind:", dict(kinds))

    print(f"\nintervals closed at the previous observation {previous}: {len(closed)}")
    opened_symbols = {(r[0]) for r in opened}
    for symbol, isin, series, vfrom, vto in closed:
        note = "reappears under another series" if symbol in opened_symbols else "absent from latest list"
        print(f"  {symbol:14s} {series:6s} {vfrom} -> {vto}  ({note})")

    if kinds["SUSPECT_SPLIT"]:
        print(f"\nFAIL: {kinds['SUSPECT_SPLIT']} interval(s) split where they should have been extended")
        return 6
    print("\nOK: no suspect interval splits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
