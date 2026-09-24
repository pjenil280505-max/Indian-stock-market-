"""Versioned migrations and the read-only connection check, against real
PostgreSQL. Skipped when TEST_DATABASE_URL is not set.

The central scenario is production as it stood before Phase 1a: every table
already exists and holds data, but there is no schema_migrations table.
Applying the migrations there must record them and change no data.
"""
import importlib.util
import os
from datetime import date
from pathlib import Path

import pytest

from src.db.migrate import (
    MIGRATION_LOCK_KEY,
    MigrationError,
    SchemaNotCurrent,
    apply_pending,
    applied,
    discover,
    pending,
    require_current_schema,
)
from src.db.repository import Repository, connect, data_fingerprint

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

ROOT = Path(__file__).resolve().parents[1]
ALL_TABLES = (
    "integrity_findings, adjustment_factors, corporate_actions, daily_bars_adjusted,"
    " daily_bars_raw, universe_snapshots, universe_membership, ingestion_log,"
    " ingestion_runs, symbols, schema_migrations"
)
OLD_STATUS_CHECK = "CHECK (status IN ('loaded','no_data','failed'))"


def _reset(conn):
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {ALL_TABLES} CASCADE")
    conn.commit()


def _baseline_without_bookkeeping(conn):
    """Reproduce pre-Phase-1a production: tables present, no schema_migrations."""
    with conn.cursor() as cur:
        cur.execute(discover()[0].sql)
    conn.commit()


def _status_constraint(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT oid, pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = 'ingestion_log_status'"
        )
        row = cur.fetchone()
    conn.rollback()
    return row


def _catalog_snapshot(conn):
    """Everything DDL could change: relations, constraints, indexes, columns."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n"
            "          ON n.oid = c.relnamespace WHERE n.nspname = 'public'),"
            "       (SELECT count(*) FROM pg_constraint),"
            "       (SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
            "          JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'),"
            "       to_regclass('schema_migrations') IS NOT NULL"
        )
        row = cur.fetchone()
    conn.rollback()
    return row


def _seed(repo):
    """Some market data, as in production."""
    from test_database import raw, uni  # shared fixtures-as-functions

    rows = [uni("RELIANCE", "INE002A01018"), uni("TCS", "INE467B01029")]
    ids = repo.upsert_symbols(rows)
    repo.record_universe_snapshot(date(2026, 9, 21), rows, ids)
    repo.record_universe_snapshot(date(2026, 9, 22), rows, ids)
    by_ticker = repo.symbol_ids_by_ticker()
    repo.upsert_raw_bars(
        [raw("RELIANCE", date(2026, 9, 21)), raw("TCS", date(2026, 9, 21), close=4100.0)],
        by_ticker, "nse_bhavcopy",
    )
    repo.mark_ingestion("nse_daily", date(2026, 9, 21), "loaded", 2, repo.start_run("test"))


@pytest.fixture()
def conn():
    c = connect(TEST_DB)
    _reset(c)
    yield c
    c.rollback()
    c.close()


class TestFreshDatabase:
    def test_applies_all_and_records_them(self, conn):
        done = apply_pending(conn)
        assert [m.version for m in done] == [m.version for m in discover()]
        assert set(applied(conn)) == {m.version for m in discover()}
        assert pending(conn) == []

    def test_second_apply_is_a_no_op(self, conn):
        apply_pending(conn)
        before = _catalog_snapshot(conn)
        assert apply_pending(conn) == []
        assert _catalog_snapshot(conn) == before


class TestExistingProductionShape:
    """The exact situation on Neon at the start of Phase 1a."""

    def test_existing_data_is_untouched(self, conn):
        _baseline_without_bookkeeping(conn)
        _seed(Repository(conn))
        before = data_fingerprint(conn)
        assert before["daily_bars_raw"][0] == 2
        assert before["universe_membership"][0] == 2

        done = apply_pending(conn)

        assert [m.version for m in done] == ["0001", "0002"]
        assert data_fingerprint(conn) == before

    def test_baseline_and_partial_status_are_no_ops_on_current_schema(self, conn):
        _baseline_without_bookkeeping(conn)
        constraint_before = _status_constraint(conn)
        catalog_before = _catalog_snapshot(conn)

        apply_pending(conn)

        # Same constraint object (same OID): 0002 did not drop and re-add it.
        assert _status_constraint(conn) == constraint_before
        # Only difference in the catalog: the schema_migrations table itself.
        after = _catalog_snapshot(conn)
        assert after[3] is True and catalog_before[3] is False

    def test_widens_an_old_status_constraint_without_losing_rows(self, conn):
        _baseline_without_bookkeeping(conn)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE ingestion_log DROP CONSTRAINT ingestion_log_status")
            cur.execute(
                f"ALTER TABLE ingestion_log ADD CONSTRAINT ingestion_log_status {OLD_STATUS_CHECK}"
            )
        conn.commit()
        repo = Repository(conn)
        run_id = repo.start_run("test")
        repo.mark_ingestion("nse_daily", date(2026, 9, 21), "loaded", 10, run_id)

        apply_pending(conn)

        assert "partial" in _status_constraint(conn)[1]
        repo.mark_ingestion("nse_daily", date(2026, 9, 22), "partial", 5, run_id)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ingestion_log")
            assert cur.fetchone()[0] == 2
        conn.rollback()


class TestSafetyChecks:
    def test_edited_migration_is_refused(self, conn):
        apply_pending(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = '0001'")
        conn.commit()
        with pytest.raises(MigrationError, match="edited after being applied"):
            pending(conn)
        with pytest.raises(MigrationError):
            apply_pending(conn)

    def test_concurrent_migrator_is_refused(self, conn):
        other = connect(TEST_DB)
        try:
            with other.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
            with pytest.raises(MigrationError, match="already running"):
                apply_pending(conn)
            assert _catalog_snapshot(conn)[3] is False  # nothing was created
        finally:
            other.close()

    def test_failed_migration_rolls_back_and_is_not_recorded(self, conn, tmp_path):
        good = discover()
        broken = tmp_path / f"{len(good) + 1:04d}_broken.sql"
        broken.write_text("CREATE TABLE phase1a_probe (x int); SELECT 1/0;")
        from src.db.migrate import Migration

        extra = Migration(f"{len(good) + 1:04d}", "broken", broken)
        with pytest.raises(MigrationError, match="broken"):
            apply_pending(conn, good + [extra])
        assert extra.version not in applied(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('phase1a_probe')")
            assert cur.fetchone()[0] is None
        conn.rollback()


class TestNormalJobGuard:
    def test_blocks_jobs_on_an_unmigrated_database(self, conn):
        _baseline_without_bookkeeping(conn)
        with pytest.raises(SchemaNotCurrent, match="Database migrations"):
            require_current_schema(conn)

    def test_guard_itself_writes_nothing(self, conn):
        _baseline_without_bookkeeping(conn)
        before = _catalog_snapshot(conn)
        with pytest.raises(SchemaNotCurrent):
            require_current_schema(conn)
        assert _catalog_snapshot(conn) == before

    def test_passes_once_migrated(self, conn):
        apply_pending(conn)
        require_current_schema(conn)  # does not raise


class TestReadOnlyConnection:
    def test_rejects_ddl(self, conn):
        apply_pending(conn)
        ro = connect(TEST_DB, read_only=True)
        try:
            import psycopg

            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                with ro.cursor() as cur:
                    cur.execute("CREATE TABLE phase1a_probe (x int)")
            ro.rollback()
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                with ro.cursor() as cur:
                    cur.execute(
                        "INSERT INTO ingestion_runs (commit_sha, status) VALUES ('x', 'running')"
                    )
        finally:
            ro.close()


def _load_check_connection():
    spec = importlib.util.spec_from_file_location(
        "check_connection", ROOT / "scripts" / "check_connection.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCheckConnectionScript:
    def test_healthy_database_changes_nothing(self, conn, monkeypatch, capsys):
        apply_pending(conn)
        _seed(Repository(conn))
        catalog, data = _catalog_snapshot(conn), data_fingerprint(conn)
        monkeypatch.setenv("DATABASE_URL", TEST_DB)

        assert _load_check_connection().main() == 0

        assert _catalog_snapshot(conn) == catalog
        assert data_fingerprint(conn) == data
        out = capsys.readouterr().out
        assert "schema: current" in out
        assert TEST_DB not in out

    def test_unmigrated_database_is_reported_not_fixed(self, conn, monkeypatch, capsys):
        """The old script silently ran DDL here. The new one must not."""
        _baseline_without_bookkeeping(conn)
        before = _catalog_snapshot(conn)
        monkeypatch.setenv("DATABASE_URL", TEST_DB)

        assert _load_check_connection().main() == 4

        assert _catalog_snapshot(conn) == before  # still no schema_migrations
        assert "BEHIND" in capsys.readouterr().out


class TestFingerprint:
    def test_detects_a_single_edited_price(self, conn):
        apply_pending(conn)
        _seed(Repository(conn))
        before = data_fingerprint(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE daily_bars_raw SET close = close + 0.05"
                " WHERE symbol_id = (SELECT min(symbol_id) FROM daily_bars_raw)"
            )
        conn.commit()
        after = data_fingerprint(conn)
        assert after["daily_bars_raw"][0] == before["daily_bars_raw"][0]  # same count
        assert after["daily_bars_raw"] != before["daily_bars_raw"]  # digest moved
