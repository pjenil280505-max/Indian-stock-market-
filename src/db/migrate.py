"""Versioned schema migrations.

Schema changes run here and nowhere else. Normal jobs (daily update, backfill,
connection check) never execute DDL: they call `require_current_schema`, which
is read-only and stops the job if a migration is pending.

Rules:
  * Migrations are `NNNN_name.sql` files in src/db/migrations, applied in
    version order, each in its own transaction.
  * An applied migration is recorded with a SHA-256 of its file. If a file
    changes after it has been applied, `pending` refuses to continue rather
    than silently diverging from what the database actually ran.
  * A session-level advisory lock stops two migrators running at once.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATION_LOCK_KEY = 7_461_992_001  # arbitrary, stable; namespaced to this project

_BOOKKEEPING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    """The schema cannot be brought up to date safely."""


class SchemaNotCurrent(RuntimeError):
    """A job was started against a database with pending migrations."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text()

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """All migration files, in version order. Rejects gaps and bad names."""
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if not match:
            raise MigrationError(f"badly named migration file: {path.name}")
        found.append(Migration(match.group(1), match.group(2), path))
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions: {versions}")
    expected = [f"{i:04d}" for i in range(1, len(found) + 1)]
    if versions != expected:
        raise MigrationError(f"migration versions must be contiguous from 0001: {versions}")
    return found


def _table_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (name,))
        return cur.fetchone()[0] is not None


def applied(conn) -> dict[str, str]:
    """{version: checksum} already applied. Read-only; empty if never migrated."""
    if not _table_exists(conn, "schema_migrations"):
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return dict(cur.fetchall())


def pending(conn, migrations: list[Migration] | None = None) -> list[Migration]:
    """Migrations not yet applied. Raises if an applied file was edited."""
    migrations = migrations if migrations is not None else discover()
    done = applied(conn)
    for m in migrations:
        if m.version in done and done[m.version] != m.checksum:
            raise MigrationError(
                f"migration {m.version}_{m.name} was edited after being applied; "
                "add a new migration instead of changing an applied one"
            )
    return [m for m in migrations if m.version not in done]


def require_current_schema(conn) -> None:
    """Read-only guard for normal jobs. Raises SchemaNotCurrent if behind."""
    outstanding = pending(conn)
    conn.rollback()  # close the read transaction; never leave one open
    if outstanding:
        names = ", ".join(f"{m.version}_{m.name}" for m in outstanding)
        raise SchemaNotCurrent(
            f"{len(outstanding)} pending migration(s): {names}. "
            "Run the 'Database migrations' workflow (mode=apply) first."
        )


def apply_pending(conn, migrations: list[Migration] | None = None) -> list[Migration]:
    """Apply outstanding migrations, each in its own transaction."""
    migrations = migrations if migrations is not None else discover()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        if not cur.fetchone()[0]:
            conn.rollback()
            raise MigrationError("another migration is already running")
    try:
        with conn.cursor() as cur:
            cur.execute(_BOOKKEEPING_DDL)
        conn.commit()

        done = []
        for m in pending(conn, migrations):
            try:
                with conn.cursor() as cur:
                    cur.execute(m.sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, name, checksum)"
                        " VALUES (%s, %s, %s)",
                        (m.version, m.name, m.checksum),
                    )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise MigrationError(f"migration {m.version}_{m.name} failed: {exc}") from exc
            done.append(m)
        return done
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
        conn.commit()
