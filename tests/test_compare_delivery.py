"""The 29 Sep revision check (scripts/compare_delivery.py): proves an unchanged
file compares IDENTICAL after a real load, and that a changed value, a
missing row and an extra row are each reported. NSE is faked; the DB test
needs TEST_DATABASE_URL."""
import importlib.util
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = os.environ.get("TEST_DATABASE_URL")
D = date(2026, 9, 29)
HEADER = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE,"
          " CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER")


def load():
    spec = importlib.util.spec_from_file_location("compare_delivery", ROOT / "scripts" / "compare_delivery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def csv_body(rows):
    return ("\n".join([HEADER] + rows) + "\n").encode()


ROWS = [
    "AAA, EQ, 29-Sep-2026, 100.05, 101.10, 103.95, 99.80, 102.00, 102.35, 101.9, 123456, 125.81234567, 4321, 61728, 50.00",
    "BBB, BE, 29-Sep-2026, 10.00, 10.10, 10.50, 9.90, 10.20, 10.25, 10.2, 1000, 0.10123, 12, -, -",
]


def test_norm_rounds_like_postgres_numeric():
    cd = load()
    assert cd.norm("turnover", 0.10123 * 100_000) == Decimal("10123.0000")
    assert cd.norm("deliv_pct", 50.00005) == Decimal("50.0001")
    assert cd.norm("volume", 5.0) == 5 and cd.norm("deliv_qty", None) is None


def test_compare_reports_each_kind_of_difference():
    cd = load()
    row = {f: 1 for f in cd.FIELDS}
    db = {("A", "EQ"): dict(row), ("GONE", "EQ"): dict(row)}
    nse = {("A", "EQ"): dict(row, deliv_qty=2), ("NEW", "EQ"): dict(row)}
    r = cd.compare(nse, db)
    assert r["diffs"] == [(("A", "EQ"), "deliv_qty", 1, 2)]
    assert r["only_db"] == [("GONE", "EQ")] and r["only_file"] == [("NEW", "EQ")]


@pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")
@pytest.mark.parametrize("served,expect", [
    (ROWS, 0),                                                    # unchanged -> identical
    ([ROWS[0].replace("61728, 50.00", "61729, 50.00"), ROWS[1]], 5),  # revised delivery qty
    ([ROWS[0]], 5),                                               # a loaded row vanished
])
def test_against_a_real_load(monkeypatch, capsys, served, expect):
    from src.db.migrate import apply_pending
    from src.db.repository import Repository, connect, data_fingerprint
    from src.sources.nse import parse_delivery_bhavcopy

    conn = connect(TEST_DB)
    with conn.cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS integrity_findings, adjustment_factors, corporate_actions,"
            " daily_bars_adjusted, daily_bars_raw, universe_snapshots, universe_membership,"
            " ingestion_log, ingestion_runs, symbols, schema_migrations CASCADE")
    conn.commit()
    apply_pending(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO symbols (isin, symbol, first_seen, last_seen) VALUES"
                    " ('INE000000001', 'AAA', %s, %s), ('INE000000002', 'BBB', %s, %s)", (D, D, D, D))
    conn.commit()
    repo = Repository(conn)
    repo.upsert_raw_bars(parse_delivery_bhavcopy(csv_body(ROWS), D), repo.symbol_ids_by_ticker(), "nse")
    before = data_fingerprint(conn)

    cd = load()

    class FakeResp:
        status = 200
        headers = {"Last-Modified": "Tue, 29 Sep 2026 15:16:05 GMT"}

        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: FakeResp(csv_body(served)))
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setattr(sys, "argv", ["compare_delivery.py", "--date", "2026-09-29"])

    assert cd.main() == expect
    out = capsys.readouterr().out
    assert "last-modified: Tue, 29 Sep 2026 15:16:05 GMT" in out
    assert ("RESULT: IDENTICAL" in out) == (expect == 0)
    assert data_fingerprint(conn) == before, "the comparison must never write"
    conn.close()
