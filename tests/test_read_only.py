"""The system must be read-only with respect to the market.

docs/PHASE_0_REPORT.md section 7: never place live trades, never connect a
broker for execution. This is enforced mechanically, not by convention, so a
future change that introduces order placement fails the build.

The Upstox Analytics Token independently cannot trade ("does not support
trading operations"), so this is defence in depth rather than the only guard.
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# Order-placement and fund-movement endpoints across Indian broker APIs.
FORBIDDEN_PATTERNS = [
    r"/order/place",
    r"/order/modify",
    r"/order/cancel",
    r"place_order",
    r"placeOrder",
    r"cancel_order",
    r"modify_order",
    r"/v2/order",
    r"/v3/order",
    r"exit_positions",
    r"/payout",
    r"square_off",
    r"squareOff",
]

FORBIDDEN_IMPORTS = ["kiteconnect", "smartapi", "dhanhq", "fyers", "breeze"]


def source_files():
    return sorted(SRC.rglob("*.py")) + sorted(SCRIPTS.rglob("*.py"))


class TestNoOrderPlacement:
    @pytest.mark.parametrize("pattern", FORBIDDEN_PATTERNS)
    def test_no_order_endpoint_anywhere(self, pattern):
        offenders = []
        for path in source_files():
            text = path.read_text()
            # Ignore this test's own pattern list if it is ever moved into src/
            for lineno, line in enumerate(text.splitlines(), 1):
                if re.search(pattern, line, re.IGNORECASE):
                    offenders.append(f"{path.name}:{lineno}")
        assert not offenders, f"order-placement pattern {pattern!r} found at {offenders}"

    @pytest.mark.parametrize("module", FORBIDDEN_IMPORTS)
    def test_no_broker_execution_sdk_imported(self, module):
        for path in source_files():
            assert module not in path.read_text().lower(), (
                f"{path.name} references broker SDK {module!r}"
            )


class TestHttpClientIsGetOnly:
    def test_no_write_verbs_exposed(self):
        from src.http_client import HttpClient

        for verb in ("post", "put", "delete", "patch"):
            assert not hasattr(HttpClient, verb)

    def test_only_get_is_used_in_sources(self):
        """Every network call in the adapters goes through the GET-only client."""
        for path in (SRC / "sources").rglob("*.py"):
            text = path.read_text()
            assert "urlopen" not in text, f"{path.name} bypasses HttpClient"
            assert "requests.post" not in text


def sql_literals(path: Path) -> list[str]:
    """String constants in a module, excluding docstrings.

    Checking raw file text would match prose in comments and docstrings, so
    this inspects only the strings that could actually reach the database.
    """
    import ast

    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


class TestRepositoryDoesNotDestroyData:
    """Data is appended and corrected, never silently removed."""

    @pytest.mark.parametrize("verb", ["DROP TABLE", "TRUNCATE", "DELETE FROM"])
    def test_no_destructive_sql_in_repository(self, verb):
        for literal in sql_literals(SRC / "db" / "repository.py"):
            assert verb not in literal.upper(), f"repository must not issue {verb}"

    def test_no_destructive_sql_in_pipeline_or_entrypoints(self):
        targets = [SRC / "pipeline.py", SCRIPTS / "daily_update.py", SCRIPTS / "backfill.py"]
        for path in targets:
            for literal in sql_literals(path):
                upper = literal.upper()
                for verb in ("DROP TABLE", "TRUNCATE", "DELETE FROM"):
                    assert verb not in upper, f"{path.name} must not issue {verb}"

    def test_the_detector_actually_catches_destructive_sql(self):
        """Guard against the check silently passing because it matches nothing."""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write('"""A docstring mentioning DELETE FROM harmlessly."""\n')
            fh.write('QUERY = "DELETE FROM symbols"\n')
            temp = Path(fh.name)
        literals = sql_literals(temp)
        assert any("DELETE FROM" in lit.upper() for lit in literals)
        assert not any("harmlessly" in lit for lit in literals), "docstring must be ignored"
        temp.unlink()
