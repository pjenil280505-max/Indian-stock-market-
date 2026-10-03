#!/usr/bin/env python3
"""Phase 2 evidence job - step 2.1: issuer identity + evidence-storage check.

    python3 scripts/phase2_update.py [--skip-storage-check]

1. Refuses to run inside the Phase 1 production window (weekdays 10:30-15:45
   UTC). It shares the database-writer lock with the daily load, and GitHub
   cancels an older PENDING run when a newer one queues in the same group, so
   a Phase 2 run inside that window could cancel a waiting daily load.
2. Stops if the schema is not current (never runs DDL itself).
3. Syncs issuers, security->issuer links and ticker aliases from symbols.
   Writes only the Phase 2 identity tables plus run bookkeeping.
4. Proves the evidence store works end to end: writes a fixed canary object
   (never overwritten), reads it back and checks its SHA-256.

Prints counts only; never connection strings or credentials.
Exit codes: 0 ok or skipped, 2 DATABASE_URL missing, 3 storage not configured
or failed, 4 identity errors.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import logging
import sys
from datetime import datetime, time, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_settings  # noqa: E402

IDENTITY_SOURCE = "phase2_identity"
WINDOW_START = time(10, 30)
WINDOW_END = time(15, 45)
CANARY = b"isr-evidence canary v1 - written by scripts/phase2_update.py\n"

log = logging.getLogger("phase2_update")


def in_production_window(now_utc: datetime) -> bool:
    """Weekday 10:30-15:45 UTC: readiness polling, the daily load and the
    15:05 staleness check all happen here."""
    return now_utc.weekday() < 5 and WINDOW_START <= now_utc.time() < WINDOW_END


def storage_check(blobs) -> str:
    sha = hashlib.sha256(CANARY).hexdigest()
    key = f"canary/{sha[:2]}/{sha}.gz"
    written = blobs.put_if_absent(key, gzip.compress(CANARY, mtime=0))
    back = gzip.decompress(blobs.get(key))
    if hashlib.sha256(back).hexdigest() != sha:
        raise RuntimeError("canary read back with a different SHA-256")
    return f"storage OK ({'written' if written else 'already present'}, verified)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-storage-check", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    now = datetime.now(timezone.utc)
    if in_production_window(now):
        print(f"SKIPPED: {now:%a %H:%M} UTC is inside the Phase 1 production window")
        return 0

    settings = load_settings()
    if not settings.has_database:
        print("FAIL: DATABASE_URL is not set")
        return 2

    from src.db.migrate import require_current_schema
    from src.db.repository import Repository, connect
    from src.identity import sync_identity

    conn = connect(settings.database_url)
    try:
        require_current_schema(conn)
        repo = Repository(conn)
        run_id = repo.start_run(settings.commit_sha)
        today = now.date()
        summary = sync_identity(conn, today)
        repo.record_findings(run_id, summary.findings)
        errors = [f for f in summary.findings if f.severity == "error"]
        repo.mark_ingestion(IDENTITY_SOURCE, today, "failed" if errors else "loaded",
                            summary.symbols_seen, run_id)
        repo.finish_run(run_id, "halted" if errors else "succeeded", summary.as_text())
        print(f"identity: {summary.as_text()}")
        for kind, n in sorted(_count(summary.findings).items()):
            print(f"  findings {kind}: {n}")
    finally:
        conn.close()

    if errors:
        print(f"FAIL: {len(errors)} identity error(s); see integrity_findings run {run_id}")
        return 4

    if args.skip_storage_check:
        print("storage check skipped")
        return 0
    from src.storage.blobs import BlobStoreError, R2BlobStore

    try:
        print(storage_check(R2BlobStore.from_env()))
    except (BlobStoreError, RuntimeError) as exc:
        print(f"FAIL: evidence storage: {exc}")
        return 3
    return 0


def _count(findings) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        out[f.check_name] = out.get(f.check_name, 0) + 1
    return out


if __name__ == "__main__":
    sys.exit(main())
