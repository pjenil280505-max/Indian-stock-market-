#!/usr/bin/env python3
"""Compare NSE's CURRENT delivery file for one date with what Neon holds. READ-ONLY.

    python3 scripts/compare_delivery.py --date 2026-09-29

Diagnostic only. Answers: did NSE revise sec_bhavdata_full after we loaded
it? It fetches the file once, parses it with the pipeline's own parser, and
compares every stored column for every (symbol, series) against
daily_bars_raw. It never writes: the connection is read-only at the server
and nothing is committed.

File values are rounded to the column scale (NUMERIC(..., 4)) the way
PostgreSQL rounds them on insert, so an unchanged file compares equal.

Exit codes: 0 identical, 2 DATABASE_URL missing, 5 differences found,
            6 file unavailable or not a delivery file (could not compare).
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import NSE_ARCHIVES, load_settings  # noqa: E402
from src.db.repository import connect  # noqa: E402
from src.sources.nse import parse_delivery_bhavcopy  # noqa: E402

FIELDS = ("open", "high", "low", "close", "prev_close", "last_price", "volume",
          "turnover", "trades", "deliv_qty", "deliv_pct")
INT_FIELDS = {"volume", "trades", "deliv_qty"}
SCALE = Decimal("0.0001")
SAMPLE = 25


def norm(field: str, value):
    """A value as Neon would store it. Pure."""
    if value is None:
        return None
    if field in INT_FIELDS:
        return int(value)
    return Decimal(repr(value) if isinstance(value, float) else str(value)).quantize(SCALE, ROUND_HALF_UP)


def compare(file_rows: dict, db_rows: dict) -> dict:
    """Both maps are (symbol, series) -> {field: value}. Pure."""
    only_file = sorted(set(file_rows) - set(db_rows))
    only_db = sorted(set(db_rows) - set(file_rows))
    diffs = []  # (key, field, neon, nse)
    for key in sorted(set(file_rows) & set(db_rows)):
        for f in FIELDS:
            a, b = norm(f, db_rows[key][f]), norm(f, file_rows[key][f])
            if a != b:
                diffs.append((key, f, a, b))
    return {"only_file": only_file, "only_db": only_db, "diffs": diffs,
            "common": len(set(file_rows) & set(db_rows))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    d = args.date

    settings = load_settings()
    if not settings.has_database:
        print("FAIL: DATABASE_URL is not set")
        return 2

    import urllib.request

    from src.http_client import HttpClient  # imported late: offline tests never need it

    headers = {}

    def opener(req, timeout):  # the pipeline's client, keeping the response headers
        resp = urllib.request.urlopen(req, timeout=timeout)
        headers.update({k.lower(): v for k, v in resp.headers.items()})
        return resp

    url = f"{NSE_ARCHIVES}/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"
    try:
        resp = HttpClient(opener=opener).get_optional(url)
    except Exception as exc:  # noqa: BLE001 - report, never guess
        print(f"UNAVAILABLE: {url} -> {exc}")
        return 6
    if resp is None or not resp.ok:
        print(f"UNAVAILABLE: {url} -> {getattr(resp, 'status', '404 not found')}")
        return 6
    print(f"file: {url}")
    print(f"  last-modified: {headers.get('last-modified', 'not sent')}; bytes: {len(resp.body):,};"
          f" sha256: {hashlib.sha256(resp.body).hexdigest()[:16]}")
    bars = parse_delivery_bhavcopy(resp.body, d)
    if not bars:
        print("UNAVAILABLE: response parsed to 0 bars (not a delivery file)")
        return 6
    file_rows = {(b.symbol, b.series): {f: getattr(b, f) for f in FIELDS} for b in bars}

    conn = connect(settings.database_url, read_only=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT s.symbol, r.series, {', '.join('r.' + f for f in FIELDS)}"
                " FROM daily_bars_raw r JOIN symbols s USING (symbol_id) WHERE r.trade_date = %s",
                (d,),
            )
            db_rows = {(row[0], row[1]): dict(zip(FIELDS, row[2:])) for row in cur.fetchall()}
            cur.execute("SELECT min(ingested_at), max(ingested_at), array_agg(DISTINCT source)"
                        " FROM daily_bars_raw WHERE trade_date = %s", (d,))
            first, last, sources = cur.fetchone()
            cur.execute("SELECT status, rows_loaded FROM ingestion_log"
                        " WHERE source = 'nse_daily' AND data_date = %s", (d,))
            log_row = cur.fetchone()
            cur.execute("SELECT symbol FROM symbols")
            known = {row[0] for row in cur.fetchall()}
        conn.rollback()
    finally:
        conn.close()

    print(f"neon: {len(db_rows):,} rows for {d}; ingested {first} .. {last}; source {sources};"
          f" ingestion_log {log_row}")
    print(f"nse file: {len(file_rows):,} EQ/BE/BZ rows")

    result = compare(file_rows, db_rows)
    unknown = [k for k in result["only_file"] if k[0] not in known]
    print(f"compared rows: {result['common']:,}")
    print(f"in file, not in neon: {len(result['only_file'])}"
          f" ({len(unknown)} have a ticker absent from symbols, which the loader skips by design)")
    for key in result["only_file"][:SAMPLE]:
        print(f"  FILE-ONLY {key[0]} {key[1]}{'  (unknown ticker)' if key in unknown else ''}")
    print(f"in neon, not in file: {len(result['only_db'])}")
    for key in result["only_db"][:SAMPLE]:
        print(f"  NEON-ONLY {key[0]} {key[1]}")

    by_field = {f: 0 for f in FIELDS}
    for _, f, _, _ in result["diffs"]:
        by_field[f] += 1
    rows_changed = len({k for k, _, _, _ in result["diffs"]})
    print(f"field differences: {len(result['diffs'])} across {rows_changed} rows")
    print("  per field: " + ", ".join(f"{f}={n}" for f, n in by_field.items()))
    for (sym, ser), f, a, b in result["diffs"][:SAMPLE]:
        print(f"  DIFF {sym} {ser} {f}: neon={a} nse_now={b}")

    known_only_file = len(result["only_file"]) - len(unknown)
    if result["diffs"] or result["only_db"] or known_only_file:
        print("RESULT: DIFFERENT - the current NSE file does not match what we loaded")
        return 5
    print("RESULT: IDENTICAL - every loaded row matches the current NSE file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
