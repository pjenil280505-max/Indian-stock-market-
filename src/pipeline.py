"""Daily data-update pipeline. Read-only with respect to the market.

Resumability and idempotency, which the scheduler requires (report section 1.3
- scheduled runs are delayed 15-30 min and can be skipped entirely):

  * Every (source, date) outcome is recorded in ingestion_log.
  * A run computes outstanding dates from that log, so a missed day is caught
    up automatically on the next run without manual intervention.
  * Every write is an upsert, so re-running a loaded date changes nothing.
  * A 404 from the archive means "not a trading day" and is recorded as
    'no_data', so holidays are learned once and never re-fetched.

Phase 1 scope: data only. No signals, no ranking, no strategy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import integrity
from .sources.nse import NseSource
from .sources.upstox import UpstoxSource

log = logging.getLogger(__name__)

NSE_SOURCE = "nse_daily"
UNIVERSE_SOURCE = "nse_universe"
UPSTOX_SOURCE = "upstox_daily"

# How far back a routine run will try to catch up. A longer gap is a backfill,
# which is run deliberately rather than by a scheduled job.
DEFAULT_CATCHUP_DAYS = 10


@dataclass
class RunSummary:
    run_id: int | None = None
    universe_symbols: int = 0
    dates_attempted: list[date] = field(default_factory=list)
    dates_loaded: list[date] = field(default_factory=list)
    dates_no_data: list[date] = field(default_factory=list)
    dates_failed: list[date] = field(default_factory=list)
    raw_bars_written: int = 0
    adjusted_bars_written: int = 0
    corporate_actions_written: int = 0
    factors_written: int = 0
    findings: list = field(default_factory=list)
    halted: bool = False

    @property
    def error_findings(self):
        return [f for f in self.findings if f.severity == "error"]

    def as_text(self) -> str:
        lines = [
            f"run_id={self.run_id}",
            f"universe_symbols={self.universe_symbols}",
            f"dates_loaded={[d.isoformat() for d in self.dates_loaded]}",
            f"dates_no_data={[d.isoformat() for d in self.dates_no_data]}",
            f"dates_failed={[d.isoformat() for d in self.dates_failed]}",
            f"raw_bars_written={self.raw_bars_written}",
            f"adjusted_bars_written={self.adjusted_bars_written}",
            f"corporate_actions={self.corporate_actions_written}",
            f"adjustment_factors={self.factors_written}",
            f"findings={len(self.findings)} (errors={len(self.error_findings)})",
            f"halted={self.halted}",
        ]
        return "\n".join(lines)


def outstanding_dates(
    repo, source: str, today: date, catchup_days: int = DEFAULT_CATCHUP_DAYS
) -> list[date]:
    """Weekdays in the catch-up window that have not settled yet.

    Today is excluded: the archive is published after the close, and a run
    fetching the current day would race publication.
    """
    start = today - timedelta(days=catchup_days)
    end = today - timedelta(days=1)
    expected = set(integrity.candidate_dates(start, end))
    settled = repo.settled_dates(source)
    return integrity.missing_trading_dates(expected, settled)


class DailyPipeline:
    """Orchestrates one data-update run."""

    def __init__(self, repo, nse: NseSource | None = None, upstox: UpstoxSource | None = None):
        self.repo = repo
        self.nse = nse or NseSource()
        self.upstox = upstox

    def run(self, today: date | None = None, catchup_days: int = DEFAULT_CATCHUP_DAYS) -> RunSummary:
        today = today or date.today()
        summary = RunSummary()
        summary.run_id = self.repo.start_run(self._commit_sha())

        try:
            # Step 1: archive the universe FIRST. This is the survivorship-bias
            # control and it cannot be reconstructed later, so it must not be
            # blocked by a later step failing.
            symbol_ids = self._update_universe(today, summary)

            # Step 2: raw bars for every outstanding date.
            self._update_raw_bars(today, catchup_days, symbol_ids, summary)

            # Step 3: corporate actions (best effort).
            self._update_corporate_actions(symbol_ids, summary)

            # Step 4: adjusted bars, if a token is configured.
            if self.upstox is not None:
                self._update_adjusted_bars(summary)

            # Step 5: cross-source integrity, and persist adjustment factors.
            self._run_integrity(summary)

            if integrity.has_errors(summary.findings):
                summary.halted = True
                self.repo.finish_run(
                    summary.run_id, "halted",
                    f"{len(summary.error_findings)} integrity error(s)",
                )
            else:
                self.repo.finish_run(summary.run_id, "succeeded", summary.as_text())
        except Exception as exc:  # noqa: BLE001 - record then re-raise
            self.repo.finish_run(summary.run_id, "failed", repr(exc))
            raise

        return summary

    # --- steps -----------------------------------------------------------

    def _update_universe(self, today: date, summary: RunSummary) -> dict[str, int]:
        rows = self.nse.fetch_universe()
        symbol_ids = self.repo.upsert_symbols(rows)
        written = self.repo.record_universe_snapshot(today, rows, symbol_ids)
        summary.universe_symbols = written
        self.repo.mark_ingestion(UNIVERSE_SOURCE, today, "loaded", written, summary.run_id)
        log.info("universe snapshot %s: %d symbols", today, written)
        return symbol_ids

    def _update_raw_bars(self, today, catchup_days, symbol_ids, summary: RunSummary) -> None:
        pending = outstanding_dates(self.repo, NSE_SOURCE, today, catchup_days)
        summary.dates_attempted = list(pending)
        ticker_ids = self.repo.symbol_ids_by_ticker()

        for trade_date in pending:
            try:
                bars = self.nse.fetch_daily_bars(trade_date)
            except Exception as exc:  # noqa: BLE001 - one bad date must not kill the run
                log.warning("fetch failed for %s: %s", trade_date, exc)
                self.repo.mark_ingestion(NSE_SOURCE, trade_date, "failed", 0, summary.run_id)
                summary.dates_failed.append(trade_date)
                continue

            if bars is None:
                # Archive 404: a market holiday. Learn it once.
                self.repo.mark_ingestion(NSE_SOURCE, trade_date, "no_data", 0, summary.run_id)
                summary.dates_no_data.append(trade_date)
                continue

            findings = integrity.validate_batch(bars, trade_date)
            summary.findings.extend(findings)
            if integrity.has_errors(findings):
                self.repo.mark_ingestion(NSE_SOURCE, trade_date, "failed", 0, summary.run_id)
                summary.dates_failed.append(trade_date)
                continue

            written = self.repo.upsert_raw_bars(bars, ticker_ids, self.nse.name)
            summary.raw_bars_written += written
            self.repo.mark_ingestion(NSE_SOURCE, trade_date, "loaded", written, summary.run_id)
            summary.dates_loaded.append(trade_date)
            log.info("loaded %d raw bars for %s", written, trade_date)

    def _update_corporate_actions(self, symbol_ids, summary: RunSummary) -> None:
        actions = self.nse.fetch_corporate_actions()
        if not actions:
            return
        ticker_ids = self.repo.symbol_ids_by_ticker()
        summary.corporate_actions_written = self.repo.upsert_corporate_actions(
            actions, ticker_ids, self.nse.name
        )

    def _update_adjusted_bars(self, summary: RunSummary) -> None:
        """Adjusted bars for dates just loaded.

        Phase 1 keeps this narrow on purpose: full adjusted backfill is a
        separate, chunked, resumable job (scripts/backfill.py), not something
        a scheduled daily run should attempt inside the 6-hour job limit.
        """
        if not summary.dates_loaded:
            return
        log.info("adjusted-bar refresh is handled by scripts/backfill.py in Phase 1")

    def _run_integrity(self, summary: RunSummary) -> None:
        for trade_date in summary.dates_loaded:
            paired = self.repo.paired_closes(trade_date)
            if not paired:
                continue
            findings, factors = integrity.cross_source_findings(paired, trade_date)
            summary.findings.extend(findings)
            summary.factors_written += self.repo.upsert_adjustment_factors(factors)

        if summary.findings:
            self.repo.record_findings(summary.run_id, summary.findings)

    @staticmethod
    def _commit_sha() -> str:
        import os

        return os.environ.get("GITHUB_SHA", "local")
