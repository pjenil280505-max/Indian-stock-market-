"""Phase 2.1: issuer identity, the evidence register, object storage, the job.

Offline tests need nothing. Database tests need TEST_DATABASE_URL and run the
real migrations against a real PostgreSQL server.
"""
import gzip
import hashlib
import importlib.util
import os
import re
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.evidence import EvidenceError, EvidenceStore, storage_key
from src.identity import isin_check_digit, isin_problem, issuer_code
from src.storage.blobs import (
    BlobStoreError, LocalBlobStore, R2BlobStore, check_key, sigv4_authorization,
)

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = os.environ.get("TEST_DATABASE_URL")
WORKFLOW = ROOT / ".github" / "workflows" / "phase2-update.yml"
ENDPOINT = "https://" + "a1" * 16 + ".r2.cloudflarestorage.com"
SECRET = "very-secret-key-value"


def load_job():
    spec = importlib.util.spec_from_file_location("phase2_update", ROOT / "scripts" / "phase2_update.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- identity: pure ----------------------------------------------------------

def test_isin_check_digit_matches_published_isins():
    assert isin_check_digit("US037833100") == 5     # Apple
    assert isin_check_digit("INE002A0101") == 8     # Reliance
    assert isin_check_digit("INE467B0102") == 9     # TCS


@pytest.mark.parametrize("isin,problem", [
    ("INE002A01018", None), (None, "missing"), ("INE002A0101", "malformed"),
    ("ine002a01018", "malformed"), ("US0378331005", "not Indian"), ("INE002A01019", "bad check digit"),
])
def test_isin_problem(isin, problem):
    assert isin_problem(isin) == problem


def test_issuer_code_keeps_a_company_together_and_excludes_funds():
    assert issuer_code("INE002A01018") == issuer_code("IN9002A01024") == "E002A"  # equity + partly paid
    assert issuer_code("INF209K01VA3") is None   # a mutual fund is not a company
    assert issuer_code("INE002A01019") is None   # invalid check digit is never mapped


# ---- storage: pure -------------------------------------------------------------

def test_sigv4_matches_the_aws_published_example():
    """AWS S3 docs, 'Example: GET Object' - an independent known answer."""
    auth = sigv4_authorization(
        "GET", "https://examplebucket.s3.amazonaws.com/test.txt",
        {"host": "examplebucket.s3.amazonaws.com", "range": "bytes=0-9",
         "x-amz-content-sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
         "x-amz-date": "20130524T000000Z"},
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "20130524T000000Z",
        region="us-east-1")
    assert auth.endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in auth


@pytest.mark.parametrize("key", ["../etc/passwd", "/abs/key", "nsex/../../x", "Source/ab/c.gz", "plain"])
def test_unsafe_keys_are_refused(key):
    with pytest.raises(BlobStoreError):
        check_key(key)


@pytest.mark.parametrize("endpoint", [
    "http://" + "a1" * 16 + ".r2.cloudflarestorage.com",          # not TLS
    "https://" + "a1" * 16 + ".r2.cloudflarestorage.com.evil.io",  # lookalike host
    "https://s3.amazonaws.com", "",
])
def test_r2_refuses_any_other_endpoint(endpoint):
    with pytest.raises(BlobStoreError):
        R2BlobStore(endpoint, "isr-evidence", "id", SECRET)


def test_r2_requires_credentials_and_never_shows_them():
    with pytest.raises(BlobStoreError):
        R2BlobStore(ENDPOINT, "isr-evidence", "", "")
    store = R2BlobStore(ENDPOINT, "isr-evidence", "key-id", SECRET)
    assert SECRET not in repr(store)
    for verb in ("delete", "delete_object", "remove", "overwrite"):
        assert not hasattr(store, verb)


class FakeR2:
    """Records requests; answers HEAD/PUT/GET from a dict."""

    def __init__(self, put_status=200):
        self.objects, self.calls, self.put_status = {}, [], put_status

    def __call__(self, req, timeout):
        self.calls.append(req)
        key = req.full_url.split("/isr-evidence/", 1)[1]
        method = req.get_method()
        if method == "HEAD" and key not in self.objects or method == "GET" and key not in self.objects:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        if method == "PUT":
            if self.put_status != 200:
                raise urllib.error.HTTPError(req.full_url, self.put_status, "x", {}, None)
            self.objects[key] = req.data
        body = self.objects.get(key, b"") if method == "GET" else b""
        return type("R", (), {"status": 200, "read": lambda s: body, "__enter__": lambda s: s,
                              "__exit__": lambda s, *a: False})()


def r2(fake):
    clock = lambda: datetime(2026, 10, 3, 1, 2, 3, tzinfo=timezone.utc)  # noqa: E731
    return R2BlobStore(ENDPOINT, "isr-evidence", "key-id", SECRET, opener=fake, clock=clock)


def test_r2_put_once_then_never_again_and_only_to_its_endpoint():
    fake = FakeR2()
    store = r2(fake)
    assert store.put_if_absent("nse_test/ab/x.gz", b"data") is True
    assert store.put_if_absent("nse_test/ab/x.gz", b"other") is False  # never replaced
    assert store.get("nse_test/ab/x.gz") == b"data"
    methods = [c.get_method() for c in fake.calls]
    assert methods == ["HEAD", "PUT", "HEAD", "GET"]
    for c in fake.calls:
        assert c.full_url.startswith(ENDPOINT + "/isr-evidence/")
        assert all(SECRET not in str(v) for v in c.headers.values())
        assert c.get_header("Authorization").startswith("AWS4-HMAC-SHA256 Credential=key-id/20261003/auto/s3/")
    put = fake.calls[1]
    assert put.get_header("If-none-match") == "*"
    assert put.get_header("X-amz-content-sha256") == hashlib.sha256(b"data").hexdigest()


def test_r2_errors_name_the_status_not_the_secret():
    with pytest.raises(BlobStoreError) as exc:
        r2(FakeR2(put_status=403)).put_if_absent("nse_test/ab/x.gz", b"data")
    assert "403" in str(exc.value) and SECRET not in str(exc.value)
    assert r2(FakeR2(put_status=412)).put_if_absent("nse_test/ab/y.gz", b"data") is False


def test_local_store_never_overwrites(tmp_path):
    store = LocalBlobStore(tmp_path)
    assert store.put_if_absent("s/ab/k.gz", b"one") is True
    assert store.put_if_absent("s/ab/k.gz", b"two") is False
    assert store.get("s/ab/k.gz") == b"one"


# ---- the job and its workflow -----------------------------------------------

@pytest.mark.parametrize("stamp,inside", [
    ("2026-10-05 10:29", False), ("2026-10-05 10:30", True), ("2026-10-05 15:44", True),
    ("2026-10-05 15:45", False), ("2026-10-03 12:00", False),  # Saturday
    ("2026-10-06 00:31", False),
])
def test_production_window(stamp, inside):
    now = datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    assert load_job().in_production_window(now) is inside


def test_storage_check_round_trip(tmp_path):
    job = load_job()
    assert "written" in job.storage_check(LocalBlobStore(tmp_path))
    assert "already present" in job.storage_check(LocalBlobStore(tmp_path))


def test_workflow_is_a_locked_writer_with_narrow_secrets_outside_the_window():
    text = WORKFLOW.read_text()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert re.search(r"^concurrency:\n  group: database-writer\n  cancel-in-progress: false$", code, re.M)
    assert "permissions:\n  contents: read\n" in code
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", code)) == {
        "DATABASE_URL", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"}
    (cron,) = re.findall(r"cron: '([^']+)'", code)
    minute, hour = cron.split()[:2]
    assert int(hour) < 4, "must be scheduled far from the 10:30-15:45 UTC window, allowing GitHub's delay"


def test_phase2_runtime_code_has_no_ddl_or_destructive_sql():
    from test_phase1a_guards import DDL, string_literals

    for rel in ("src/identity.py", "src/evidence.py", "src/storage/blobs.py", "scripts/phase2_update.py"):
        for lineno, literal in string_literals(ROOT / rel):
            assert not DDL.search(literal), f"{rel}:{lineno} contains DDL"
            for verb in ("DELETE FROM", "TRUNCATE", "DROP "):
                assert verb not in literal.upper(), f"{rel}:{lineno} contains {verb}"


# ---- database ---------------------------------------------------------------------

needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")
PHASE1 = ("integrity_findings, adjustment_factors, corporate_actions, daily_bars_adjusted, daily_bars_raw,"
          " universe_snapshots, universe_membership, ingestion_log, ingestion_runs, symbols, schema_migrations")
PHASE2 = "source_documents, symbol_aliases, symbol_issuer, issuers"


@pytest.fixture()
def conn():
    from src.db.migrate import apply_pending
    from src.db.repository import connect

    c = connect(TEST_DB)
    with c.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {PHASE2}, {PHASE1} CASCADE")
    c.commit()
    apply_pending(c)
    yield c
    c.rollback()
    c.close()


def seed_symbols(conn, rows):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO symbols (isin, symbol, name, first_seen, last_seen) VALUES (%s, %s, %s, %s, %s)",
            [(isin, sym, name, date(2026, 9, 1), date(2026, 9, 1)) for isin, sym, name in rows])
    conn.commit()


def table(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    conn.rollback()
    return rows


@needs_db
def test_migration_0003_adds_tables_and_changes_no_phase1_data():
    """Production shape: 0001+0002 applied with data, then 0003 alone."""
    from src.db.migrate import apply_pending, discover
    from src.db.repository import Repository, connect, data_fingerprint
    from test_migrations import _seed

    c = connect(TEST_DB)
    with c.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {PHASE2}, {PHASE1} CASCADE")
    c.commit()
    apply_pending(c, discover()[:2])
    _seed(Repository(c))
    phase1_columns = table(c, "SELECT table_name, column_name, data_type FROM information_schema.columns"
                              " WHERE table_schema = 'public' ORDER BY 1, 2")
    before = data_fingerprint(c)

    done = apply_pending(c)

    assert [m.version for m in done] == ["0003"]
    assert data_fingerprint(c) == before
    after = table(c, "SELECT table_name, column_name, data_type FROM information_schema.columns"
                     " WHERE table_schema = 'public' AND table_name NOT IN"
                     " ('issuers', 'symbol_issuer', 'symbol_aliases', 'source_documents') ORDER BY 1, 2")
    assert after == phase1_columns, "0003 must not alter any existing table"
    c.close()


@needs_db
def test_identity_sync_maps_companies_and_is_idempotent(conn):
    from src.identity import sync_identity

    seed_symbols(conn, [
        ("INE002A01018", "RELIANCE", "Reliance Industries Limited"),
        ("IN9002A01024", "RELIANCEPP", "Reliance Industries Limited PP"),
        ("INE467B01029", "TCS", "Tata Consultancy Services Limited"),
        ("INE000X01019", "BADISIN", "Bad Check Digit Ltd"),
        ("INF209K01VA3", "SOMEETF", "Some ETF"),
    ])
    s = sync_identity(conn, date(2026, 10, 6))
    kinds = sorted(f.check_name for f in s.findings)
    assert kinds == ["invalid_isin", "issuer_multi_security", "non_company_isin"]
    assert s.links_created == 3 and s.issuers_upserted == 2
    assert table(conn, "SELECT issuer_code, name FROM issuers ORDER BY 1") == [
        ("E002A", "Reliance Industries Limited"), ("E467B", "Tata Consultancy Services Limited")]

    snapshot = table(conn, "SELECT * FROM symbol_aliases ORDER BY 1, 2")
    again = sync_identity(conn, date(2026, 10, 6))
    assert again.links_created == 0
    assert table(conn, "SELECT * FROM symbol_aliases ORDER BY 1, 2") == snapshot


@needs_db
def test_ticker_aliases_extend_and_record_a_rename(conn):
    from src.identity import sync_identity

    seed_symbols(conn, [("INE467B01029", "TCS", "Tata Consultancy Services Limited")])
    sync_identity(conn, date(2026, 10, 6))
    sync_identity(conn, date(2026, 10, 7))
    with conn.cursor() as cur:
        cur.execute("UPDATE symbols SET symbol = 'TCSNEW' WHERE isin = 'INE467B01029'")
    conn.commit()
    sync_identity(conn, date(2026, 10, 9))
    assert table(conn, "SELECT ticker, valid_from, valid_to FROM symbol_aliases ORDER BY valid_from") == [
        ("TCS", date(2026, 10, 6), date(2026, 10, 7)),
        ("TCSNEW", date(2026, 10, 9), date(2026, 10, 9)),
    ]


@needs_db
def test_evidence_is_stored_once_versioned_and_verified(conn, tmp_path):
    blobs = LocalBlobStore(tmp_path)
    ev = EvidenceStore(conn, blobs)
    first = ev.put(source="nse_results_xbrl", source_record_id="SEQ1", body=b"<xbrl>v1</xbrl>",
                   url="https://nsearchives.nseindia.com/corporate/xbrl/A.xml", content_type="application/xml")
    assert first.new and first.storage_key == storage_key("nse_results_xbrl", first.sha256)
    assert gzip.decompress(blobs.get(first.storage_key)) == b"<xbrl>v1</xbrl>"

    same = ev.put(source="nse_results_xbrl", source_record_id="SEQ1", body=b"<xbrl>v1</xbrl>")
    assert (same.doc_id, same.new) == (first.doc_id, False)

    revised = ev.put(source="nse_results_xbrl", source_record_id="SEQ1", body=b"<xbrl>v2</xbrl>")
    assert revised.new and revised.doc_id != first.doc_id
    assert [d for d, _ in ev.versions("nse_results_xbrl", "SEQ1")] == [first.doc_id, revised.doc_id]
    assert ev.read(first.doc_id) == b"<xbrl>v1</xbrl>"  # the old version is still there

    (tmp_path / first.storage_key).write_bytes(gzip.compress(b"tampered", mtime=0))
    with pytest.raises(EvidenceError, match="do not match"):
        ev.read(first.doc_id)


@needs_db
@pytest.mark.parametrize("statement", [
    "UPDATE source_documents SET sha256 = repeat('0', 64)",
    "UPDATE source_documents SET url = 'https://elsewhere'",
    "UPDATE source_documents SET storage_key = 'x/y/z.gz'",
    "DELETE FROM source_documents",
])
def test_evidence_rows_are_immutable_in_the_database(conn, tmp_path, statement):
    import psycopg

    EvidenceStore(conn, LocalBlobStore(tmp_path)).put(source="nse_test", source_record_id="1", body=b"x")
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute(statement)
    conn.rollback()
    assert table(conn, "SELECT count(*) FROM source_documents") == [(1,)]


@needs_db
def test_processing_status_may_change_but_only_with_a_reason(conn, tmp_path):
    import psycopg

    doc = EvidenceStore(conn, LocalBlobStore(tmp_path)).put(source="nse_test", source_record_id="1", body=b"x")
    with conn.cursor() as cur:
        cur.execute("UPDATE source_documents SET parse_status = 'rejected', reject_reason = 'no_xbrl'"
                    " WHERE doc_id = %s", (doc.doc_id,))
    conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute("UPDATE source_documents SET parse_status = 'rejected', reject_reason = NULL")
    conn.rollback()


@needs_db
def test_a_failed_upload_registers_nothing(conn):
    class Broken:
        def put_if_absent(self, key, data, content_type="application/gzip"):
            raise BlobStoreError("PUT -> HTTP 503")

    with pytest.raises(BlobStoreError):
        EvidenceStore(conn, Broken()).put(source="nse_test", source_record_id="1", body=b"x")
    conn.rollback()
    assert table(conn, "SELECT count(*) FROM source_documents") == [(0,)]


@needs_db
@pytest.mark.parametrize("kwargs,match", [
    ({"source": "NSE Results"}, "invalid source"),
    ({"source_record_id": ""}, "1-200"),
    ({"body": b"x" * (25 * 1024 * 1024 + 1)}, "larger than"),
])
def test_evidence_rejects_bad_input(conn, tmp_path, kwargs, match):
    args = {"source": "nse_test", "source_record_id": "1", "body": b"x", **kwargs}
    with pytest.raises(EvidenceError, match=match):
        EvidenceStore(conn, LocalBlobStore(tmp_path)).put(**args)
