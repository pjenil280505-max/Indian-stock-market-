#!/usr/bin/env python3
"""Run or inspect versioned schema migrations.

    python3 scripts/migrate.py status   # read-only: applied/pending + data fingerprint
    python3 scripts/migrate.py apply    # apply pending, proving data unchanged

`apply` fingerprints the protected tables (daily_bars_raw, universe_membership,
symbols) before and after, and exits non-zero if any of them changed. Schema
migrations must never alter existing market data.

Exit codes: 0 ok, 2 DATABASE_URL missing, 3 migration error,
            4 pending migrations (status mode), 5 protected data changed.
Never prints the connection string.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_settings  # noqa: E402
from src.db.migrate import MigrationError, apply_pending, applied, discover, pending  # noqa: E402
from src.db.repository import connect, data_fingerprint  # noqa: E402


def show_fingerprint(label: str, fp: dict) -> None:
    print(f"{label}:")
    for table, (rows, lo, hi, digest) in fp.items():
        print(f"  {table:22s} rows={rows:>10,}  range={lo}..{hi}  digest={digest}")


def protected_changes(before: dict, after: dict) -> list[str]:
    """Tables whose contents differ. A table that existed before must be
    identical after; a table a migration newly created must be empty."""
    changed = [t for t, fp in before.items() if after.get(t) != fp]
    changed += [t for t, fp in after.items() if t not in before and fp[0] != 0]
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("status", "apply"))
    args = parser.parse_args()

    settings = load_settings()
    if not settings.has_database:
        print("FAIL: DATABASE_URL is not set")
        return 2

    migrations = discover()

    if args.mode == "status":
        conn = connect(settings.database_url, read_only=True)
        try:
            done = applied(conn)
            outstanding = pending(conn, migrations)
            conn.rollback()
            # Same fingerprint `apply` prints, over a read-only connection, so
            # data preservation can be checked before/after any operation.
            show_fingerprint("protected data", data_fingerprint(conn))
        except MigrationError as exc:
            print(f"FAIL: {exc}")
            return 3
        finally:
            conn.close()
        for m in migrations:
            print(f"  {m.version}_{m.name:40s} {'applied' if m.version in done else 'PENDING'}")
        print(f"{len(migrations) - len(outstanding)} applied, {len(outstanding)} pending")
        return 4 if outstanding else 0

    conn = connect(settings.database_url)
    try:
        before = data_fingerprint(conn)
        show_fingerprint("protected data BEFORE", before)
        try:
            done = apply_pending(conn, migrations)
        except MigrationError as exc:
            print(f"FAIL: {exc}")
            return 3
        after = data_fingerprint(conn)
        show_fingerprint("protected data AFTER", after)
    finally:
        conn.close()

    for m in done:
        print(f"applied {m.version}_{m.name}")
    print(f"{len(done)} migration(s) applied")

    changed = protected_changes(before, after)
    if changed:
        print(f"FAIL: protected data changed during migration: {', '.join(changed)}")
        return 5
    print("OK: protected data identical before and after")
    return 0


if __name__ == "__main__":
    sys.exit(main())
