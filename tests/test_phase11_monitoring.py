"""Phase 1.1: independent freshness check and the GitHub Issue alert script.

Offline except the DB-backed freshness test (skipped without TEST_DATABASE_URL).
The alert script runs against a FAKE `gh` on PATH, so nothing touches GitHub.
"""
import importlib.util
import os
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALERT = ROOT / "scripts" / "raise_alert.sh"
TEST_DB = os.environ.get("TEST_DATABASE_URL")


def load_freshness():
    spec = importlib.util.spec_from_file_location("freshness_check", ROOT / "scripts" / "freshness_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- freshness: pure logic ------------------------------------------------

def test_expected_dates_are_prior_weekdays_newest_first():
    fc = load_freshness()
    # 2026-09-29 is a Tuesday: previous 7 days hold 5 weekdays
    assert fc.expected_dates(date(2026, 9, 29), 7) == [
        date(2026, 9, 28), date(2026, 9, 25), date(2026, 9, 24), date(2026, 9, 23), date(2026, 9, 22),
    ]


def test_assess_classifies_every_case():
    fc = load_freshness()
    d = [date(2026, 9, 28), date(2026, 9, 25), date(2026, 9, 24), date(2026, 9, 23), date(2026, 9, 22)]
    statuses = {d[0]: "loaded", d[1]: "partial", d[2]: "no_data", d[3]: "failed"}  # d[4] has no row
    published = {d[3]: True, d[4]: False}
    asked = []

    def is_published(x):
        asked.append(x)
        return published[x]

    got = {x: v for x, v, _ in fc.assess(d, statuses, is_published)}
    assert got == {d[0]: "ok", d[1]: "ok", d[2]: "ok", d[3]: "STALE", d[4]: "ok"}
    assert asked == [d[3], d[4]], "NSE is asked only about dates not accounted for"


def test_assess_never_guesses_when_nse_is_unreachable():
    fc = load_freshness()

    def boom(_):
        raise RuntimeError("403")

    [(_, verdict, note)] = fc.assess([date(2026, 9, 28)], {}, boom)
    assert verdict == "UNVERIFIED"
    assert "unreachable" in note


# ---- freshness: against a real database, with NSE faked -------------------

needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")


@needs_db
def test_freshness_main_detects_a_published_unloaded_day_and_writes_nothing(monkeypatch, capsys):
    from src.db.migrate import apply_pending
    from src.db.repository import Repository, connect, data_fingerprint

    conn = connect(TEST_DB)
    with conn.cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS integrity_findings, adjustment_factors, corporate_actions,"
            " daily_bars_adjusted, daily_bars_raw, universe_snapshots, universe_membership,"
            " ingestion_log, ingestion_runs, symbols, schema_migrations CASCADE")
    conn.commit()
    apply_pending(conn)
    repo = Repository(conn)
    run_id = repo.start_run("test")
    fc = load_freshness()
    today = date(2026, 9, 29)
    dates = fc.expected_dates(today, 7)
    for d in dates[1:]:
        repo.mark_ingestion("nse_daily", d, "loaded", 2500, run_id)
    before = data_fingerprint(conn)

    class FakeNse:
        def fetch_daily_bars(self, d):
            return ["bar"] if d == dates[0] else None  # only the missing day is "published"

    import src.sources.nse as nse_module
    monkeypatch.setattr(nse_module, "NseSource", FakeNse)
    monkeypatch.setattr(fc, "datetime", type("T", (), {"now": staticmethod(lambda tz: __import__("datetime").datetime(2026, 9, 29, 3, 30, tzinfo=tz))}))
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setattr(sys, "argv", ["freshness_check.py"])

    assert fc.main() == 7
    out = capsys.readouterr().out
    assert f"STALE {dates[0]}" in out
    assert data_fingerprint(conn) == before
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ingestion_log")
        assert cur.fetchone()[0] == len(dates) - 1
    conn.close()


# ---- alert script with a fake gh -----------------------------------------

@pytest.fixture()
def fake_gh(tmp_path):
    """A `gh` that records its argv, and lists one open issue if told to."""
    log = tmp_path / "gh.log"
    existing = tmp_path / "existing.txt"
    gh = tmp_path / "gh"
    gh.write_text(f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
if [ "$1 $2" = "issue list" ]; then cat "{existing}" 2>/dev/null || true; fi
""")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return tmp_path, log, existing


def run_alert(tmp_path, **env):
    base = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "o/r", "ALERT_OWNER": "pjenil280505-max", "GH_TOKEN": "x",
        "ALERT_KIND": "test_alert", "ALERT_DATE": "2026-09-29", "ALERT_DETAIL": "", "RUN_URL": "u",
    }
    base.update(env)
    return subprocess.run(["bash", str(ALERT)], env=base, capture_output=True, text=True)


def test_alert_opens_an_issue_mentioning_the_owner(fake_gh):
    tmp, log, _ = fake_gh
    r = run_alert(tmp, ALERT_KIND="nse_blocked", ALERT_DETAIL="HTTP 403")
    assert r.returncode == 0, r.stderr
    calls = log.read_text()
    assert "issue create --repo o/r --title [pipeline-alert] nse_blocked 2026-09-29" in calls
    assert "@pjenil280505-max" in calls and "HTTP 403" in calls


def test_alert_comments_on_an_existing_issue_instead_of_duplicating(fake_gh):
    tmp, log, existing = fake_gh
    existing.write_text("42\n")
    assert run_alert(tmp).returncode == 0
    calls = log.read_text()
    assert "issue comment 42" in calls and "issue create" not in calls


@pytest.mark.parametrize("env", [
    {"ALERT_KIND": "drop_tables"},
    {"ALERT_KIND": "test_alert; rm -rf /"},
    {"ALERT_DATE": "2026-9-29"},
    {"ALERT_DATE": "$(whoami)"},
    {"ALERT_OWNER": "a b"},
])
def test_alert_rejects_bad_input_before_calling_gh(fake_gh, env):
    tmp, log, _ = fake_gh
    assert run_alert(tmp, **env).returncode != 0
    assert not log.exists() or log.read_text() == ""


def test_alert_strips_mentions_and_code_fences_from_detail(fake_gh):
    tmp, log, _ = fake_gh
    assert run_alert(tmp, ALERT_DETAIL="ping @everyone `rm` \n newline").returncode == 0
    calls = log.read_text()
    assert "@everyone" not in calls and "`rm`" not in calls


def test_alert_fails_loudly_if_github_cannot_list_issues(tmp_path):
    """Never guess: a failing `gh issue list` must fail the workflow run
    (visible), not silently create a duplicate or drop the alert."""
    gh = tmp_path / "gh"
    gh.write_text("#!/usr/bin/env bash\nexit 1\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    assert run_alert(tmp_path).returncode != 0


def test_unknown_kind_is_rejected_by_the_allowlist_itself(fake_gh):
    tmp, _, _ = fake_gh
    r = run_alert(tmp, ALERT_KIND="drop_tables")
    assert "refusing unknown alert kind" in r.stderr
