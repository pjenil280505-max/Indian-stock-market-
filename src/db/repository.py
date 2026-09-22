"""Database access. Every write is idempotent.

Re-running the pipeline for a date that is already loaded must be a no-op,
because scheduled runs are delayed, retried and caught up (report section 1.3).
Idempotency is enforced by primary keys plus ON CONFLICT, not by the caller
remembering what it did.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(database_url: str):
    """Open a psycopg connection. Imported lazily so the package imports
    without a driver present (offline tests never touch the database)."""
    import psycopg

    return psycopg.connect(database_url)


def apply_schema(conn) -> None:
    """Create tables if absent. Safe to run on every start."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA_PATH.read_text())
    conn.commit()


class Repository:
    """Read/write access to the market-data store.

    Exposes no delete or truncate operation: data is appended and corrected,
    never silently removed.
    """

    def __init__(self, conn):
        self.conn = conn

    # --- run bookkeeping -------------------------------------------------

    def start_run(self, commit_sha: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ingestion_runs (status, commit_sha) VALUES ('running', %s)"
                " RETURNING run_id",
                (commit_sha,),
            )
            run_id = cur.fetchone()[0]
        self.conn.commit()
        return run_id

    def finish_run(self, run_id: int, status: str, notes: str = "") -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE ingestion_runs SET finished_at = now(), status = %s, notes = %s"
                " WHERE run_id = %s",
                (status, notes[:4000], run_id),
            )
        self.conn.commit()

    # --- symbols and universe -------------------------------------------

    def upsert_symbols(self, rows) -> dict[str, int]:
        """Insert or refresh symbols, keyed on ISIN. Returns {isin: symbol_id}.

        first_seen is preserved across runs; last_seen advances. A ticker
        rename updates `symbol` while the ISIN - and therefore all history -
        stays put.
        """
        if not rows:
            return {}
        today = date.today()
        payload = [
            (r.isin, r.symbol, r.name, r.face_value, r.listing_date or today, today)
            for r in rows
        ]
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO symbols (isin, symbol, name, face_value, first_seen, last_seen)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (isin) DO UPDATE SET
                    symbol     = EXCLUDED.symbol,
                    name       = COALESCE(EXCLUDED.name, symbols.name),
                    face_value = COALESCE(EXCLUDED.face_value, symbols.face_value),
                    first_seen = LEAST(symbols.first_seen, EXCLUDED.first_seen),
                    last_seen  = GREATEST(symbols.last_seen, EXCLUDED.last_seen)
                """,
                payload,
            )
        self.conn.commit()
        return self.symbol_ids_by_isin([r.isin for r in rows])

    def symbol_ids_by_isin(self, isins) -> dict[str, int]:
        if not isins:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT isin, symbol_id FROM symbols WHERE isin = ANY(%s)", (list(isins),)
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    def symbol_ids_by_ticker(self) -> dict[str, int]:
        """Map current ticker -> symbol_id, for files that carry no ISIN."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT symbol, symbol_id FROM symbols")
            return {row[0]: row[1] for row in cur.fetchall()}

    def record_universe_snapshot(self, snapshot_date: date, rows, symbol_ids) -> int:
        """Archive point-in-time universe membership.

        This is the survivorship-bias control. It cannot be reconstructed
        after the fact, so it runs before anything else in the pipeline.
        """
        payload = [
            (snapshot_date, symbol_ids[r.isin], r.series, r.listing_date)
            for r in rows
            if r.isin in symbol_ids
        ]
        if not payload:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO universe_snapshots (snapshot_date, symbol_id, series, listing_date)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (snapshot_date, symbol_id) DO UPDATE SET
                    series = EXCLUDED.series,
                    listing_date = COALESCE(EXCLUDED.listing_date,
                                            universe_snapshots.listing_date)
                """,
                payload,
            )
        self.conn.commit()
        return len(payload)

    def universe_on(self, snapshot_date: date) -> list[tuple[int, str, str]]:
        """What was listed on a given date: (symbol_id, symbol, series)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.symbol_id, s.symbol, u.series
                FROM universe_snapshots u
                JOIN symbols s USING (symbol_id)
                WHERE u.snapshot_date = %s
                ORDER BY s.symbol
                """,
                (snapshot_date,),
            )
            return cur.fetchall()

    # --- bars ------------------------------------------------------------

    def upsert_raw_bars(self, bars, symbol_ids, source: str) -> int:
        payload = [
            (
                symbol_ids[b.symbol], b.trade_date, b.series, b.open, b.high, b.low, b.close,
                b.prev_close, b.last_price, b.volume, b.turnover, b.trades,
                b.deliv_qty, b.deliv_pct, source,
            )
            for b in bars
            if b.symbol in symbol_ids
        ]
        if not payload:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO daily_bars_raw (
                    symbol_id, trade_date, series, open, high, low, close,
                    prev_close, last_price, volume, turnover, trades,
                    deliv_qty, deliv_pct, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol_id, trade_date, series) DO UPDATE SET
                    open = EXCLUDED.open, high = EXCLUDED.high,
                    low = EXCLUDED.low, close = EXCLUDED.close,
                    prev_close = EXCLUDED.prev_close, last_price = EXCLUDED.last_price,
                    volume = EXCLUDED.volume, turnover = EXCLUDED.turnover,
                    trades = EXCLUDED.trades,
                    deliv_qty = COALESCE(EXCLUDED.deliv_qty, daily_bars_raw.deliv_qty),
                    deliv_pct = COALESCE(EXCLUDED.deliv_pct, daily_bars_raw.deliv_pct),
                    source = EXCLUDED.source, ingested_at = now()
                """,
                payload,
            )
        self.conn.commit()
        return len(payload)

    def upsert_adjusted_bars(self, bars, symbol_ids, source: str) -> int:
        payload = [
            (symbol_ids[b.isin], b.trade_date, b.open, b.high, b.low, b.close, b.volume, source)
            for b in bars
            if b.isin in symbol_ids
        ]
        if not payload:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO daily_bars_adjusted (
                    symbol_id, trade_date, open, high, low, close, volume, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol_id, trade_date) DO UPDATE SET
                    open = EXCLUDED.open, high = EXCLUDED.high,
                    low = EXCLUDED.low, close = EXCLUDED.close,
                    volume = EXCLUDED.volume, source = EXCLUDED.source,
                    ingested_at = now()
                """,
                payload,
            )
        self.conn.commit()
        return len(payload)

    def upsert_corporate_actions(self, actions, symbol_ids, source: str) -> int:
        payload = [
            (symbol_ids[a.symbol], a.ex_date, a.purpose, a.face_value, source)
            for a in actions
            if a.symbol in symbol_ids
        ]
        if not payload:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO corporate_actions (symbol_id, ex_date, purpose, face_value, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (symbol_id, ex_date, purpose) DO UPDATE SET
                    face_value = COALESCE(EXCLUDED.face_value, corporate_actions.face_value)
                """,
                payload,
            )
        self.conn.commit()
        return len(payload)

    def upsert_adjustment_factors(self, factors) -> int:
        """factors: iterable of (symbol_id, trade_date, factor)."""
        payload = list(factors)
        if not payload:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO adjustment_factors (symbol_id, trade_date, factor)
                VALUES (%s, %s, %s)
                ON CONFLICT (symbol_id, trade_date) DO UPDATE SET factor = EXCLUDED.factor
                """,
                payload,
            )
        self.conn.commit()
        return len(payload)

    # --- ingestion log: resumability ------------------------------------

    def mark_ingestion(
        self, source: str, data_date: date, status: str, rows: int, run_id: int
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingestion_log (source, data_date, status, rows_loaded, run_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (source, data_date) DO UPDATE SET
                    status = EXCLUDED.status, rows_loaded = EXCLUDED.rows_loaded,
                    run_id = EXCLUDED.run_id, updated_at = now()
                """,
                (source, data_date, status, rows, run_id),
            )
        self.conn.commit()

    def settled_dates(self, source: str) -> set[date]:
        """Dates needing no further work: loaded, or known non-trading days.

        'failed' is deliberately excluded so a failure is retried next run.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT data_date FROM ingestion_log"
                " WHERE source = %s AND status IN ('loaded', 'no_data')",
                (source,),
            )
            return {row[0] for row in cur.fetchall()}

    def last_loaded_date(self, source: str) -> date | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT max(data_date) FROM ingestion_log"
                " WHERE source = %s AND status = 'loaded'",
                (source,),
            )
            return cur.fetchone()[0]

    # --- integrity -------------------------------------------------------

    def record_findings(self, run_id: int, findings) -> int:
        payload = [
            (run_id, f.check_name, f.severity, f.symbol_id, f.trade_date, f.detail[:4000])
            for f in findings
        ]
        if not payload:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO integrity_findings
                    (run_id, check_name, severity, symbol_id, trade_date, detail)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                payload,
            )
        self.conn.commit()
        return len(payload)

    def paired_closes(self, trade_date: date):
        """(symbol_id, raw_close, adj_close, raw_volume, adj_volume) for a date.

        Feeds the cross-source adjustment check.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.symbol_id, r.close, a.close, r.volume, a.volume
                FROM daily_bars_raw r
                JOIN daily_bars_adjusted a
                  ON a.symbol_id = r.symbol_id AND a.trade_date = r.trade_date
                WHERE r.trade_date = %s
                """,
                (trade_date,),
            )
            return cur.fetchall()

    def counts(self) -> dict[str, int]:
        tables = [
            "symbols", "universe_snapshots", "daily_bars_raw",
            "daily_bars_adjusted", "corporate_actions", "adjustment_factors",
            "ingestion_log", "integrity_findings",
        ]
        out: dict[str, int] = {}
        with self.conn.cursor() as cur:
            for table in tables:
                cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed list
                out[table] = cur.fetchone()[0]
        return out
