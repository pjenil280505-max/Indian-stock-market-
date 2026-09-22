"""Database integration tests against a real PostgreSQL instance.

Skipped automatically when TEST_DATABASE_URL is not set, so the offline suite
still runs anywhere. Neon is ordinary PostgreSQL, so behaviour verified here
carries over.
"""
import os
from dataclasses import dataclass
from datetime import date

import pytest

from src.db.repository import Repository, apply_schema, connect

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")


@dataclass
class UniverseRow:
    symbol: str
    name: str
    series: str
    listing_date: date | None
    isin: str
    face_value: float | None


@dataclass
class RawBar:
    symbol: str
    isin: str | None
    series: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    prev_close: float | None
    last_price: float | None
    volume: int
    turnover: float | None
    trades: int | None
    deliv_qty: int | None
    deliv_pct: float | None


@dataclass
class AdjBar:
    isin: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


def uni(symbol, isin, series="EQ"):
    return UniverseRow(symbol, f"{symbol} Ltd", series, date(2000, 1, 1), isin, 10.0)


def raw(symbol, d, close=105.0, volume=1000, series="EQ"):
    """A structurally valid bar built around `close`, so the OHLC CHECK holds."""
    high = close * 1.05
    low = close * 0.95
    return RawBar(symbol, None, series, d, close, high, low, close,
                  close, close, volume, 1e6, 500, 400, 40.0)


@pytest.fixture()
def repo():
    conn = connect(TEST_DB)
    with conn.cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS integrity_findings, adjustment_factors,"
            " corporate_actions, daily_bars_adjusted, daily_bars_raw,"
            " universe_snapshots, ingestion_log, ingestion_runs, symbols CASCADE"
        )
    conn.commit()
    apply_schema(conn)
    yield Repository(conn)
    conn.close()


class TestSchema:
    def test_schema_applies_cleanly_twice(self, repo):
        """apply_schema runs on every start, so it must be re-runnable."""
        apply_schema(repo.conn)
        assert repo.counts()["symbols"] == 0


class TestSymbolIdentity:
    def test_upsert_returns_ids_keyed_on_isin(self, repo):
        ids = repo.upsert_symbols([uni("RELIANCE", "INE002A01018")])
        assert set(ids) == {"INE002A01018"}

    def test_upsert_is_idempotent(self, repo):
        rows = [uni("RELIANCE", "INE002A01018"), uni("TCS", "INE467B01029")]
        repo.upsert_symbols(rows)
        repo.upsert_symbols(rows)
        assert repo.counts()["symbols"] == 2

    def test_ticker_rename_preserves_identity(self, repo):
        """History must survive a ticker rename - identity is the ISIN."""
        first = repo.upsert_symbols([uni("OLDNAME", "INE002A01018")])
        second = repo.upsert_symbols([uni("NEWNAME", "INE002A01018")])
        assert first["INE002A01018"] == second["INE002A01018"]
        assert repo.counts()["symbols"] == 1
        assert repo.symbol_ids_by_ticker() == {"NEWNAME": first["INE002A01018"]}


class TestPointInTimeUniverse:
    def test_snapshot_is_recorded(self, repo):
        rows = [uni("A", "INE0A"), uni("B", "INE0B")]
        ids = repo.upsert_symbols(rows)
        assert repo.record_universe_snapshot(date(2026, 9, 21), rows, ids) == 2

    def test_snapshot_is_idempotent(self, repo):
        rows = [uni("A", "INE0A")]
        ids = repo.upsert_symbols(rows)
        repo.record_universe_snapshot(date(2026, 9, 21), rows, ids)
        repo.record_universe_snapshot(date(2026, 9, 21), rows, ids)
        assert repo.counts()["universe_snapshots"] == 1

    def test_membership_is_reconstructible_per_date(self, repo):
        """The survivorship-bias control: what was listed on date D."""
        day1 = [uni("A", "INE0A"), uni("B", "INE0B")]
        day2 = [uni("A", "INE0A")]  # B delisted
        ids1 = repo.upsert_symbols(day1)
        repo.record_universe_snapshot(date(2026, 9, 21), day1, ids1)
        ids2 = repo.upsert_symbols(day2)
        repo.record_universe_snapshot(date(2026, 9, 22), day2, ids2)

        assert [r[1] for r in repo.universe_on(date(2026, 9, 21))] == ["A", "B"]
        assert [r[1] for r in repo.universe_on(date(2026, 9, 22))] == ["A"]

    def test_delisted_symbol_retains_its_history(self, repo):
        """A delisted name must stay queryable, or backtests inherit bias."""
        rows = [uni("GONE", "INE0G")]
        ids = repo.upsert_symbols(rows)
        repo.record_universe_snapshot(date(2026, 9, 21), rows, ids)
        repo.upsert_raw_bars([raw("GONE", date(2026, 9, 21))],
                             repo.symbol_ids_by_ticker(), "nse")
        # Next day it is absent from the universe...
        repo.record_universe_snapshot(date(2026, 9, 22), [], {})
        assert repo.universe_on(date(2026, 9, 22)) == []
        # ...but its bar is still there.
        assert repo.counts()["daily_bars_raw"] == 1


class TestBarIdempotency:
    def test_reinserting_the_same_bars_is_a_noop(self, repo):
        repo.upsert_symbols([uni("A", "INE0A")])
        ids = repo.symbol_ids_by_ticker()
        bars = [raw("A", date(2026, 9, 21))]
        assert repo.upsert_raw_bars(bars, ids, "nse") == 1
        assert repo.upsert_raw_bars(bars, ids, "nse") == 1
        assert repo.counts()["daily_bars_raw"] == 1

    def test_reingest_corrects_a_revised_value(self, repo):
        repo.upsert_symbols([uni("A", "INE0A")])
        ids = repo.symbol_ids_by_ticker()
        repo.upsert_raw_bars([raw("A", date(2026, 9, 21), close=105.0)], ids, "nse")
        repo.upsert_raw_bars([raw("A", date(2026, 9, 21), close=106.0)], ids, "nse")
        with repo.conn.cursor() as cur:
            cur.execute("SELECT close FROM daily_bars_raw")
            assert float(cur.fetchone()[0]) == 106.0

    def test_unknown_ticker_is_skipped_not_crashed(self, repo):
        assert repo.upsert_raw_bars([raw("NOSUCH", date(2026, 9, 21))], {}, "nse") == 0

    def test_ohlc_constraint_rejects_insane_data_at_the_database(self, repo):
        """Defence in depth: the DB refuses it even if a check is bypassed."""
        import psycopg

        repo.upsert_symbols([uni("A", "INE0A")])
        ids = repo.symbol_ids_by_ticker()
        bad = raw("A", date(2026, 9, 21))
        bad.high, bad.low = 1.0, 99.0
        with pytest.raises(psycopg.errors.CheckViolation):
            repo.upsert_raw_bars([bad], ids, "nse")
        repo.conn.rollback()

    def test_adjusted_bars_are_idempotent(self, repo):
        ids = repo.upsert_symbols([uni("A", "INE0A")])
        bars = [AdjBar("INE0A", date(2026, 9, 21), 100, 110, 95, 105, 1000)]
        repo.upsert_adjusted_bars(bars, ids, "upstox")
        repo.upsert_adjusted_bars(bars, ids, "upstox")
        assert repo.counts()["daily_bars_adjusted"] == 1

    def test_raw_and_adjusted_coexist_for_the_same_day(self, repo):
        """They answer different questions and must never overwrite each other."""
        ids = repo.upsert_symbols([uni("A", "INE0A")])
        repo.upsert_raw_bars([raw("A", date(2026, 9, 21), close=2655.70)],
                             repo.symbol_ids_by_ticker(), "nse")
        repo.upsert_adjusted_bars(
            [AdjBar("INE0A", date(2026, 9, 21), 1327.85, 1400.0, 1300.0, 1327.85, 2000)],
            ids, "upstox")
        paired = repo.paired_closes(date(2026, 9, 21))
        assert len(paired) == 1
        assert float(paired[0][1]) / float(paired[0][2]) == pytest.approx(2.0)


class TestIngestionLogResumability:
    def test_loaded_and_no_data_both_settle(self, repo):
        run = repo.start_run("abc123")
        repo.mark_ingestion("nse_daily", date(2026, 9, 21), "loaded", 2300, run)
        repo.mark_ingestion("nse_daily", date(2026, 9, 19), "no_data", 0, run)
        assert repo.settled_dates("nse_daily") == {date(2026, 9, 21), date(2026, 9, 19)}

    def test_failed_dates_do_not_settle_so_they_retry(self, repo):
        run = repo.start_run("abc123")
        repo.mark_ingestion("nse_daily", date(2026, 9, 21), "failed", 0, run)
        assert repo.settled_dates("nse_daily") == set()

    def test_marking_is_idempotent_and_updates_status(self, repo):
        run = repo.start_run("abc123")
        repo.mark_ingestion("nse_daily", date(2026, 9, 21), "failed", 0, run)
        repo.mark_ingestion("nse_daily", date(2026, 9, 21), "loaded", 2300, run)
        assert repo.counts()["ingestion_log"] == 1
        assert repo.settled_dates("nse_daily") == {date(2026, 9, 21)}

    def test_last_loaded_date_ignores_failures(self, repo):
        run = repo.start_run("abc123")
        repo.mark_ingestion("nse_daily", date(2026, 9, 18), "loaded", 10, run)
        repo.mark_ingestion("nse_daily", date(2026, 9, 21), "failed", 0, run)
        assert repo.last_loaded_date("nse_daily") == date(2026, 9, 18)

    def test_sources_are_tracked_independently(self, repo):
        run = repo.start_run("abc123")
        repo.mark_ingestion("nse_daily", date(2026, 9, 21), "loaded", 1, run)
        assert repo.settled_dates("upstox_daily") == set()


class TestRunBookkeeping:
    def test_run_lifecycle_is_recorded(self, repo):
        run = repo.start_run("deadbeef")
        repo.finish_run(run, "succeeded", "all good")
        with repo.conn.cursor() as cur:
            cur.execute(
                "SELECT status, commit_sha, finished_at IS NOT NULL"
                " FROM ingestion_runs WHERE run_id = %s", (run,))
            assert cur.fetchone() == ("succeeded", "deadbeef", True)

    def test_findings_are_persisted_against_the_run(self, repo):
        from src.integrity import Finding

        run = repo.start_run("abc")
        written = repo.record_findings(
            run, [Finding("volume_factor_mismatch", "warning", "detail", None, date(2026, 9, 21))])
        assert written == 1
        assert repo.counts()["integrity_findings"] == 1


class TestAdjustmentFactors:
    def test_factors_persist_and_are_idempotent(self, repo):
        ids = repo.upsert_symbols([uni("A", "INE0A")])
        sid = ids["INE0A"]
        repo.upsert_adjustment_factors([(sid, date(2024, 10, 25), 2.0)])
        repo.upsert_adjustment_factors([(sid, date(2024, 10, 25), 2.0)])
        assert repo.counts()["adjustment_factors"] == 1

    def test_non_positive_factor_is_rejected(self, repo):
        import psycopg

        ids = repo.upsert_symbols([uni("A", "INE0A")])
        with pytest.raises(psycopg.errors.CheckViolation):
            repo.upsert_adjustment_factors([(ids["INE0A"], date(2024, 10, 25), 0.0)])
        repo.conn.rollback()
