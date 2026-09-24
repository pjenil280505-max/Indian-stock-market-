#!/usr/bin/env python3
"""Preflight check for the Neon (or any PostgreSQL) connection.

Safe to run in CI logs: it NEVER prints the connection string, password,
host or any token. It reports only non-sensitive facts - server version,
database size, migration status, and row counts.

READ-ONLY. The connection is opened with every transaction set READ ONLY at
the server, so this script cannot execute DDL or change any row even by
mistake. Schema changes belong to scripts/migrate.py and nothing else.

    python3 scripts/check_connection.py

Exit codes: 0 healthy, 2 DATABASE_URL missing, 3 connection failed,
            4 schema behind (pending migrations), 5 migration history invalid.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.migrate import MigrationError, pending  # noqa: E402
from src.db.repository import Repository, connect  # noqa: E402

# Neon's free tier storage allowance. Used for a headroom warning only.
FREE_TIER_BYTES = 0.5 * 1000**3


def describe_target(url: str) -> str:
    """A non-sensitive description of where we are connecting.

    Deliberately reports only the provider shape, never the host, user,
    password or database name.
    """
    lowered = url.lower()
    if "neon.tech" in lowered:
        return "Neon (serverless PostgreSQL)"
    if "localhost" in lowered or "127.0.0.1" in lowered or "host=/" in lowered:
        return "local PostgreSQL"
    return "PostgreSQL"


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("FAIL: DATABASE_URL is not set.")
        print("      Add it as a GitHub Actions repository secret named DATABASE_URL.")
        return 2

    print(f"target: {describe_target(url)}")
    if "sslmode" not in url.lower():
        # Neon requires TLS; its own connection strings include sslmode=require.
        print("warning: connection string does not specify sslmode; Neon requires sslmode=require")

    try:
        conn = connect(url, read_only=True)
    except Exception as exc:
        # Print the exception TYPE only. Driver messages can echo the host or user.
        print(f"FAIL: could not connect ({type(exc).__name__})")
        print("      Check the secret is the full Neon connection string, pooled or direct.")
        return 3

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0].split(",")[0]
            cur.execute("SELECT pg_database_size(current_database())")
            size = cur.fetchone()[0]

        print(f"connected: {version}")
        print(f"database size: {size / 1e6:.1f} MB "
              f"({size / FREE_TIER_BYTES * 100:.1f}% of a 0.5 GB free tier)")

        try:
            outstanding = pending(conn)
        except MigrationError as exc:
            print(f"FAIL: migration history invalid: {exc}")
            return 5
        if outstanding:
            names = ", ".join(f"{m.version}_{m.name}" for m in outstanding)
            print(f"schema: BEHIND - {len(outstanding)} pending migration(s): {names}")
            print("        Run the 'Database migrations' workflow (mode=apply).")
        else:
            print("schema: current (no pending migrations)")

        repo = Repository(conn)
        counts = repo.counts()
        print("row counts:")
        for table, count in counts.items():
            print(f"  {table:22s} {count:>10,}")

        last = repo.last_loaded_date("nse_daily")
        print(f"last loaded trading date: {last or 'none yet'}")
        headroom = FREE_TIER_BYTES - size
        print(f"free-tier headroom: {headroom / 1e6:.0f} MB")
        if headroom < 0.1 * FREE_TIER_BYTES:
            print("warning: under 10% headroom - stop backfilling and review scope")
        conn.rollback()  # end the read-only transaction; nothing to commit
    finally:
        conn.close()

    if outstanding:
        print("\nFAIL: schema is behind; normal jobs will refuse to run")
        return 4
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
