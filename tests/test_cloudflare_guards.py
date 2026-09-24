"""Phase 1b guards for the Cloudflare dispatcher. Offline.

The Worker is a trigger only. These tests fail the build if a change would
let it reach the database, run the production schedule, target a
workflow other than the probe or the daily update, or commit a credential.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare" / "dispatcher"
WORKFLOWS = ROOT / ".github" / "workflows"
PRODUCTION_CRONS = ("41 11 * * 1-5", "17 14 * * 1-5")


def worker_source_files():
    return [p for p in WORKER.rglob("*") if p.is_file() and "node_modules" not in p.parts
            and ".wrangler" not in p.parts and p.name != "package-lock.json"]


class TestProductionIsOff:
    def test_production_flag_false(self):
        toml = (WORKER / "wrangler.toml").read_text()
        assert re.search(r'^PRODUCTION_ENABLED = "false"$', toml, re.M)
        assert re.search(r'^PRODUCTION_CRONS = ""$', toml, re.M)

    def test_no_production_cron_scheduled_on_cloudflare(self):
        toml = (WORKER / "wrangler.toml").read_text()
        crons = re.findall(r'^crons = \[(.*)\]$', toml, re.M)
        assert len(crons) == 1
        for prod in PRODUCTION_CRONS:
            assert prod not in crons[0]

    def test_test_cron_is_at_most_daily(self):
        """Safe test schedule: fixed minute and hour, so at most once a day."""
        toml = (WORKER / "wrangler.toml").read_text()
        for cron in re.findall(r'"([^"]+)"', re.findall(r'^crons = \[(.*)\]$', toml, re.M)[0]):
            minute, hour = cron.split()[:2]
            assert minute.isdigit() and hour.isdigit(), cron


class TestWorkerCannotReachData:
    @pytest.mark.parametrize("needle", ["DATABASE_URL", "neon", "postgres", "nseindia", "nsearchives", "upstox"])
    def test_worker_code_has_no_data_access(self, needle):
        code = "\n".join(
            line for line in (WORKER / "src" / "index.js").read_text().splitlines()
            if not line.strip().startswith("//")
        ).lower()
        assert needle.lower() not in code

    def test_only_two_workflows_targetable(self):
        src = (WORKER / "src" / "index.js").read_text()
        targets = set(re.findall(r'"([a-z0-9-]+\.yml)"', src))
        assert targets == {"cloudflare-dispatch-probe.yml", "daily-data-update.yml"}

    def test_cloudflare_workflows_never_get_database_url(self):
        for name in ("cloudflare-worker.yml", "cloudflare-dispatch-probe.yml"):
            assert "secrets.DATABASE_URL" not in (WORKFLOWS / name).read_text()

    def test_probe_workflow_has_no_permissions_or_secrets(self):
        text = (WORKFLOWS / "cloudflare-dispatch-probe.yml").read_text()
        assert re.search(r"^permissions: \{\}$", text, re.M)
        assert "secrets." not in text
        assert "actions/checkout" not in text

    def test_probe_inputs_never_interpolated_into_shell(self):
        """Inputs come from outside GitHub; they must go through env vars."""
        text = (WORKFLOWS / "cloudflare-dispatch-probe.yml").read_text()
        run_blocks = re.findall(r"run: \|\n((?:          .*\n?)+)", text)
        assert run_blocks
        for block in run_blocks:
            assert "${{" not in block


class TestNoCommittedCredentials:
    PATTERNS = [
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"\bghp_[A-Za-z0-9]{20,}",
        r"CLOUDFLARE_API_TOKEN\s*=\s*\S",
        r"GH_DISPATCH_TOKEN\s*=\s*\S",
    ]

    @pytest.mark.parametrize("pattern", PATTERNS)
    def test_no_tokens_in_worker_tree(self, pattern):
        for path in worker_source_files():
            if path.parent.name == "test":
                continue  # unit tests use an obvious fake token
            assert not re.search(pattern, path.read_text(errors="ignore")), path

    def test_local_secret_files_are_ignored(self):
        ignored = (WORKER / ".gitignore").read_text().split()
        assert ".dev.vars" in ignored and "node_modules/" in ignored
