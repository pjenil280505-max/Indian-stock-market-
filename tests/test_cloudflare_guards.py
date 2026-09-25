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
GITHUB_FALLBACK_CRONS = ("17 14 * * 1-5",)


def worker_source_files():
    return [p for p in WORKER.rglob("*") if p.is_file() and "node_modules" not in p.parts
            and ".wrangler" not in p.parts and p.name != "package-lock.json"]


class TestProductionConfig:
    """Pins EXACTLY the production configuration approved in Addendum I.4.
    Any other schedule or flag change fails the build until reviewed."""

    APPROVED_TEST_CRON = "37 4 * * *"
    APPROVED_PROD_CRON = "*/15 11-14 * * 1-5"

    def _toml(self):
        return (WORKER / "wrangler.toml").read_text()

    def test_production_enabled_with_the_approved_cron_only(self):
        toml = self._toml()
        assert re.search(r'^PRODUCTION_ENABLED = "true"$', toml, re.M)
        assert re.search(rf'^PRODUCTION_CRONS = "{re.escape(self.APPROVED_PROD_CRON)}"$', toml, re.M)

    def test_cloudflare_triggers_are_exactly_test_plus_production(self):
        crons = re.findall(r'^crons = \[(.*)\]$', self._toml(), re.M)
        assert len(crons) == 1
        assert re.findall(r'"([^"]+)"', crons[0]) == [self.APPROVED_TEST_CRON, self.APPROVED_PROD_CRON]

    def test_production_window_is_weekdays_after_the_close(self):
        minute, hours, dom, month, dow = self.APPROVED_PROD_CRON.split()
        assert (dom, month, dow) == ("*", "*", "1-5")
        first_hour = int(hours.split("-")[0])
        assert first_hour * 60 > 10 * 60, "15:30 IST close = 10:00 UTC"

    def test_test_cron_is_at_most_daily(self):
        """Safe test schedule: fixed minute and hour, so at most once a day."""
        minute, hour = self.APPROVED_TEST_CRON.split()[:2]
        assert minute.isdigit() and hour.isdigit()

    def test_readiness_final_window_inside_the_production_window(self):
        m = re.search(r'^READINESS_FINAL_UTC = "(\d\d):(\d\d)"$', self._toml(), re.M)
        assert m and (int(m.group(1)), int(m.group(2))) == (14, 45)

    def test_cloudflare_does_not_duplicate_the_github_fallback_time(self):
        for github_cron in GITHUB_FALLBACK_CRONS:
            assert github_cron not in self._toml()


class TestWorkerCannotReachData:
    @pytest.mark.parametrize("needle", ["DATABASE_URL", "neon", "postgres", "nseindia", "nsearchives", "upstox"])
    def test_worker_code_has_no_data_access(self, needle):
        code = "\n".join(
            line for line in (WORKER / "src" / "index.js").read_text().splitlines()
            if not line.strip().startswith("//")
        ).lower()
        assert needle.lower() not in code

    @pytest.mark.parametrize("needle", ["DATABASE_URL", "neon", "postgres", "upstox"])
    def test_nse_probe_has_no_database_or_upstox_access(self, needle):
        code = "\n".join(
            line for line in (WORKER / "src" / "nse_probe.js").read_text().splitlines()
            if not line.strip().startswith("//")
        ).lower()
        assert needle.lower() not in code

    def test_nse_probe_is_get_only_and_nse_hosts_only(self):
        src = (WORKER / "src" / "nse_probe.js").read_text()
        methods = set(re.findall(r'method:\s*"([A-Z]+)"', src))
        assert methods == {"GET"}
        hosts = set(re.findall(r'https://([a-z0-9.-]+)', src))
        assert hosts == {"nsearchives.nseindia.com", "www.nseindia.com"}

    def test_only_worker_src_files_are_known(self):
        """A new source file must be reviewed against these guards."""
        names = sorted(p.name for p in (WORKER / "src").glob("*.js"))
        assert names == ["index.js", "nse_probe.js", "readiness.js"]

    def test_readiness_gate_is_read_only(self):
        """The gate observes GitHub and NSE; it never POSTs or dispatches."""
        src = (WORKER / "src" / "readiness.js").read_text()
        assert set(re.findall(r'method:\s*"([A-Z]+)"', src)) == {"GET"}
        assert set(re.findall(r'https://([a-z0-9.-]+)', src)) == {"api.github.com"}
        assert "/dispatches" not in src  # the dispatch API path
        for needle in ("DATABASE_URL", "neon", "postgres", "upstox"):
            assert needle not in src.lower()

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


class TestDailyWorkflowTagging:
    WF = WORKFLOWS / "daily-data-update.yml"

    def test_run_name_carries_cloudflare_tag(self):
        text = self.WF.read_text()
        assert "format('Daily data update (cloudflare {0} {1})', inputs.trade_date, inputs.request_id)" in text
        assert "format('Daily data update ({0})', github.event_name)" in text

    def test_new_inputs_are_optional_with_blank_defaults(self):
        """Manual and scheduled runs must keep working without them."""
        text = self.WF.read_text()
        for name in ("request_id", "trade_date"):
            block = re.search(rf"^      {name}:\n((?:        .*\n)+)", text, re.M)
            assert block, name
            assert "required: false" in block.group(1) and "default: ''" in block.group(1)

    def test_inputs_never_reach_a_shell(self):
        """request_id/trade_date come from outside; they may appear only in run-name."""
        text = self.WF.read_text()
        steps = text[text.index("steps:"):]
        assert "inputs.request_id" not in steps and "inputs.trade_date" not in steps

    def test_github_schedule_is_only_the_approved_fallback(self):
        """Approved in Addendum I.4: 41 11 removed, 17 14 (14:17 UTC) kept."""
        crons = re.findall(r"^\s*-\s*cron:\s*'([^']+)'", self.WF.read_text(), re.M)
        assert crons == ["17 14 * * 1-5"]


class TestGithubFallbackStep:
    WF = WORKFLOWS / "daily-data-update.yml"

    def _steps(self):
        text = self.WF.read_text()
        return text[text.index("    steps:"):]

    def test_fallback_check_is_the_first_step_and_schedule_only(self):
        steps = self._steps()
        first = steps.index("      - ")
        assert steps[first:].startswith("      - name: Fallback only - skip if Cloudflare already loaded today")
        block = steps[first:steps.index("      - uses: actions/checkout@v4")]
        assert "if: github.event_name == 'schedule'" in block
        assert "id: fallback" in block

    def test_fallback_uses_the_utc_date_not_ist(self):
        """The fallback lands ~18:30 UTC, which is already tomorrow in IST."""
        steps = self._steps()
        assert "day=$(date -u +%Y-%m-%d)" in steps
        assert "Asia/Kolkata" not in steps

    def test_fallback_counts_only_successful_cloudflare_runs_for_the_day(self):
        steps = self._steps()
        assert "-f status=success" in steps and "-f event=workflow_dispatch" in steps
        assert 'contains(\\"cloudflare $day\\")' in steps

    def test_every_pipeline_step_is_gated_by_the_fallback(self):
        steps = self._steps()
        body = steps[steps.index("      - uses: actions/checkout@v4"):steps.index("      - name: Report failure")]
        items = re.split(r"\n(?=      - )", body.strip("\n"))
        assert len(items) == 5
        for item in items:
            assert "if: steps.fallback.outputs.skip != 'true'" in item, item.splitlines()[0]

    def test_fallback_token_is_read_only(self):
        text = self.WF.read_text()
        perms = text[text.index("permissions:"):text.index("jobs:")]
        assert re.findall(r"^  (\w+): (\w+)", perms, re.M) == [("contents", "read"), ("actions", "read")]

    def test_repository_name_reaches_the_shell_only_via_env(self):
        steps = self._steps()
        fallback = steps[:steps.index("      - uses: actions/checkout@v4")]
        run = fallback[fallback.index("run: |"):]
        assert "${{" not in run
