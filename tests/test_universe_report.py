"""scripts/universe_report.py: classifies universe changes, read-only.

Skipped without TEST_DATABASE_URL (the structural read-only check below
runs everywhere).
"""
import ast
import importlib.util
import os
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "universe_report.py"
TEST_DB = os.environ.get("TEST_DATABASE_URL")
FRI, MON = date(2026, 9, 25), date(2026, 9, 28)


def load():
    spec = importlib.util.spec_from_file_location("universe_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_opens_a_read_only_connection_and_never_commits():
    tree = ast.parse(SCRIPT.read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "connect"]
    assert calls
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert getattr(kw.get("read_only"), "value", None) is True
    assert ".commit()" not in SCRIPT.read_text()


def test_classification_rules():
    c = load().classify
    assert c(None, None, "EQ", FRI) == "new_symbol"
    assert c("EQ", FRI, "BE", FRI) == "series_change"
    assert c("EQ", date(2026, 9, 23), "EQ", FRI) == "gap_relist"
    assert c("EQ", FRI, "EQ", FRI) == "SUSPECT_SPLIT"


needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")


@pytest.fixture()
def repo():
    from src.db.migrate import apply_pending
    from src.db.repository import Repository, connect

    conn = connect(TEST_DB)
    with conn.cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS integrity_findings, adjustment_factors, corporate_actions,"
            " daily_bars_adjusted, daily_bars_raw, universe_snapshots, universe_membership,"
            " ingestion_log, ingestion_runs, symbols, schema_migrations CASCADE"
        )
    conn.commit()
    apply_pending(conn)
    yield Repository(conn)
    conn.close()


def _scenario(repo):
    from test_database import uni

    wed = [uni("AAA", "INE0A"), uni("GAP", "INE0G")]
    fri = [uni("AAA", "INE0A"), uni("SER", "INE0S"), uni("GONE", "INE0X")]
    mon = [uni("AAA", "INE0A"), uni("SER", "INE0S", series="BE"), uni("NEW", "INE0N"), uni("GAP", "INE0G")]
    for day, rows in ((date(2026, 9, 23), wed), (FRI, fri), (MON, mon)):
        ids = repo.upsert_symbols(rows)
        repo.record_universe_snapshot(day, rows, ids)


def _run(monkeypatch, capsys, *argv):
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setattr("sys.argv", ["universe_report.py", *argv])
    code = load().main()
    return code, capsys.readouterr().out


@needs_db
def test_real_changes_are_classified(repo, monkeypatch, capsys):
    _scenario(repo)
    code, out = _run(monkeypatch, capsys, "--since", "2026-09-26", "--previous", "2026-09-25")
    assert code == 0
    assert "NEW            EQ     2026-09-28" in out and "new_symbol" in out
    assert "series_change" in out and "gap_relist" in out
    assert "SUSPECT_SPLIT" not in out.split("by kind:")[1].split("\n")[0]
    assert "GONE" in out and "absent from latest list" in out
    assert "SER            EQ" in out and "reappears under another series" in out


@needs_db
def test_split_that_should_have_been_an_extension_fails(repo, monkeypatch, capsys):
    _scenario(repo)
    # Simulate the bug: AAA's Friday interval not extended, a fresh one opened Monday.
    with repo.conn.cursor() as cur:
        cur.execute(
            "UPDATE universe_membership SET valid_to = %s"
            " WHERE symbol_id = (SELECT symbol_id FROM symbols WHERE isin = 'INE0A')", (FRI,))
        cur.execute(
            "INSERT INTO universe_membership (symbol_id, series, valid_from, valid_to)"
            " SELECT symbol_id, 'EQ', %s, %s FROM symbols WHERE isin = 'INE0A'", (MON, MON))
    repo.conn.commit()
    code, out = _run(monkeypatch, capsys, "--since", "2026-09-26", "--previous", "2026-09-25")
    assert code == 6
    assert "SUSPECT_SPLIT" in out


@needs_db
def test_report_writes_nothing(repo, monkeypatch, capsys):
    from src.db.repository import data_fingerprint

    _scenario(repo)
    before = data_fingerprint(repo.conn)
    _run(monkeypatch, capsys, "--since", "2026-09-26", "--previous", "2026-09-25")
    assert data_fingerprint(repo.conn) == before
