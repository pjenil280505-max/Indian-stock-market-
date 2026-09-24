"""Structural guards added in Phase 1a. Offline - no database needed.

1. Every workflow that can write to the database shares ONE concurrency
   group, so the daily update, the backfill and a migration can never run
   at the same time. (Before Phase 1a the daily job used its own group and
   the claim that it was excluded from the backfill was false.)
2. Normal jobs never contain or call DDL. Schema changes live only in
   src/db/migrations and are applied only by scripts/migrate.py.
3. The connection check opens a read-only connection.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
WRITER_GROUP = "database-writer"

# Code that runs in normal (non-migration) operation.
NORMAL_JOB_FILES = [
    ROOT / "scripts" / "check_connection.py",
    ROOT / "scripts" / "daily_update.py",
    ROOT / "scripts" / "backfill.py",
    ROOT / "src" / "pipeline.py",
    ROOT / "src" / "db" / "repository.py",
]

DDL = re.compile(
    r"\b(CREATE|ALTER|DROP|TRUNCATE)\s+(OR\s+REPLACE\s+)?"
    r"(TABLE|INDEX|UNIQUE|SCHEMA|VIEW|MATERIALIZED|SEQUENCE|TYPE|FUNCTION|EXTENSION|TEMP|TEMPORARY)\b"
    r"|\b(ADD|DROP)\s+(CONSTRAINT|COLUMN)\b"
    r"|\bTRUNCATE\b",
    re.IGNORECASE,
)


def string_literals(path: Path) -> list[tuple[int, str]]:
    """String constants in the file, excluding docstrings."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                getattr(body[0], "value", None), ast.Constant
            ):
                docstrings.add(id(body[0].value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def workflow_texts() -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(WORKFLOWS.glob("*.yml"))}


def concurrency_group(text: str) -> str | None:
    match = re.search(r"^concurrency:\s*\n(?:\s+#.*\n)*\s+group:\s*(\S+)", text, re.MULTILINE)
    return match.group(1) if match else None


class TestSingleWriterLock:
    def test_every_database_workflow_uses_the_writer_group(self):
        """Any workflow handed DATABASE_URL can write, so it must hold the lock."""
        db_workflows = {
            name: text for name, text in workflow_texts().items()
            if "secrets.DATABASE_URL" in text
        }
        assert db_workflows, "expected at least one workflow using DATABASE_URL"
        wrong = {
            name: concurrency_group(text) for name, text in db_workflows.items()
            if concurrency_group(text) != WRITER_GROUP
        }
        assert not wrong, f"workflows outside the {WRITER_GROUP!r} group: {wrong}"

    @pytest.mark.parametrize("name", ["daily-data-update.yml", "backfill.yml", "migrate.yml"])
    def test_known_writers_share_the_group(self, name):
        text = (WORKFLOWS / name).read_text()
        assert concurrency_group(text) == WRITER_GROUP

    @pytest.mark.parametrize("name", ["daily-data-update.yml", "backfill.yml", "migrate.yml"])
    def test_writers_never_cancel_a_running_writer(self, name):
        """cancel-in-progress: true would kill a half-finished write."""
        text = (WORKFLOWS / name).read_text()
        assert re.search(r"^\s+cancel-in-progress:\s*false\s*$", text, re.MULTILINE)

    def test_obsolete_destructive_workflow_is_gone(self):
        """The completed universe migration could DROP a table on dispatch."""
        assert not (WORKFLOWS / "migrate-universe.yml").exists()
        assert not (ROOT / "scripts" / "migrate_universe.py").exists()


class TestNoDdlInNormalJobs:
    @pytest.mark.parametrize("path", NORMAL_JOB_FILES, ids=lambda p: p.name)
    def test_no_ddl_statements(self, path):
        offenders = [
            f"{path.name}:{lineno}: {text.strip()[:60]!r}"
            for lineno, text in string_literals(path)
            if DDL.search(text)
        ]
        assert not offenders, f"DDL in a normal job: {offenders}"

    @pytest.mark.parametrize("path", NORMAL_JOB_FILES, ids=lambda p: p.name)
    def test_does_not_call_the_migrator(self, path):
        text = path.read_text()
        for name in ("apply_pending", "apply_schema", "_BOOKKEEPING_DDL"):
            assert name not in text, f"{path.name} references {name}"

    def test_schema_sql_retired(self):
        """The old always-run schema file is replaced by versioned migrations."""
        assert not (ROOT / "src" / "db" / "schema.sql").exists()

    @pytest.mark.parametrize("script", ["daily_update.py", "backfill.py"])
    def test_jobs_check_schema_before_writing(self, script):
        text = (ROOT / "scripts" / script).read_text()
        assert "require_current_schema(conn)" in text


class TestConnectionCheckIsReadOnly:
    def test_opens_a_read_only_connection(self):
        tree = ast.parse((ROOT / "scripts" / "check_connection.py").read_text())
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "connect"
        ]
        assert calls, "check_connection must call connect()"
        for call in calls:
            kw = {k.arg: k.value for k in call.keywords}
            assert "read_only" in kw and getattr(kw["read_only"], "value", None) is True

    def test_never_commits(self):
        assert ".commit()" not in (ROOT / "scripts" / "check_connection.py").read_text()


class TestMigrationFiles:
    def test_discovered_in_order(self):
        from src.db.migrate import discover

        versions = [m.version for m in discover()]
        assert versions[:2] == ["0001", "0002"]
        assert versions == sorted(versions)

    def test_rejects_badly_named_file(self, tmp_path):
        from src.db.migrate import MigrationError, discover

        (tmp_path / "0001_ok.sql").write_text("SELECT 1;")
        (tmp_path / "2_bad.sql").write_text("SELECT 1;")
        with pytest.raises(MigrationError, match="badly named"):
            discover(tmp_path)

    def test_rejects_gap_in_versions(self, tmp_path):
        from src.db.migrate import MigrationError, discover

        (tmp_path / "0001_a.sql").write_text("SELECT 1;")
        (tmp_path / "0003_c.sql").write_text("SELECT 1;")
        with pytest.raises(MigrationError, match="contiguous"):
            discover(tmp_path)

    def test_migrations_are_non_destructive(self):
        """No migration may drop or truncate a table or delete rows. A future
        destructive change needs explicit review, not a quiet file."""
        from src.db.migrate import discover

        for m in discover():
            body = "\n".join(
                line for line in m.sql.splitlines() if not line.strip().startswith("--")
            )
            for pattern in (r"\bDROP\s+TABLE\b", r"\bTRUNCATE\b", r"\bDELETE\s+FROM\b",
                            r"\bDROP\s+COLUMN\b"):
                assert not re.search(pattern, body, re.I), f"{m.path.name}: {pattern}"


class TestMigrateScriptComparison:
    def _load(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("migrate_cli", ROOT / "scripts" / "migrate.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_identical_data_passes(self):
        fp = {"daily_bars_raw": (10, "a", "b", 99)}
        assert self._load().protected_changes(fp, dict(fp)) == []

    def test_changed_digest_fails(self):
        cli = self._load()
        assert cli.protected_changes(
            {"daily_bars_raw": (10, "a", "b", 99)}, {"daily_bars_raw": (10, "a", "b", 98)}
        ) == ["daily_bars_raw"]

    def test_missing_table_after_fails(self):
        assert self._load().protected_changes({"symbols": (1, "a", "b", 1)}, {}) == ["symbols"]

    def test_newly_created_empty_table_passes(self):
        """A fresh database: tables appear during migration, with no rows."""
        assert self._load().protected_changes({}, {"symbols": (0, None, None, 0)}) == []

    def test_newly_created_table_with_rows_fails(self):
        assert self._load().protected_changes({}, {"symbols": (5, "a", "b", 1)}) == ["symbols"]
