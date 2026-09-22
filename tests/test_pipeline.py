"""Pipeline behaviour: resumability, idempotency, catch-up, halting.

Uses in-memory fakes so the logic is tested without network or database.
"""
from dataclasses import dataclass, field
from datetime import date

import pytest

from src.pipeline import NSE_SOURCE, DailyPipeline, outstanding_dates


@dataclass
class FakeBar:
    symbol: str
    trade_date: date
    series: str = "EQ"
    open: float = 100.0
    high: float = 110.0
    low: float = 95.0
    close: float = 105.0
    prev_close: float = 99.0
    last_price: float = 105.0
    volume: int = 1000
    turnover: float = 1e6
    trades: int = 10
    deliv_qty: int = 400
    deliv_pct: float = 40.0
    isin: str | None = None


@dataclass
class FakeUniverseRow:
    symbol: str
    isin: str
    series: str = "EQ"
    name: str = "Co"
    listing_date: date | None = None
    face_value: float | None = 10.0


class FakeRepo:
    """Minimal in-memory stand-in with the same contract as Repository."""

    def __init__(self, settled=None):
        self._settled = dict(settled or {})
        self.runs = []
        self.raw_bars = {}
        self.universe = {}
        self.symbols = {}
        self.findings = []
        self.factors = {}
        self.marks = []
        self.paired = {}

    def start_run(self, sha):
        self.runs.append({"sha": sha, "status": "running"})
        return len(self.runs)

    def finish_run(self, run_id, status, notes=""):
        self.runs[run_id - 1]["status"] = status

    def upsert_symbols(self, rows):
        for r in rows:
            self.symbols.setdefault(r.isin, len(self.symbols) + 1)
        return {r.isin: self.symbols[r.isin] for r in rows}

    def symbol_ids_by_ticker(self):
        return {f"SYM{i}": i for i in self.symbols.values()} | {"A": 1, "B": 2}

    def record_universe_snapshot(self, snapshot_date, rows, ids):
        self.universe[snapshot_date] = [r.isin for r in rows if r.isin in ids]
        return len(self.universe[snapshot_date])

    def upsert_raw_bars(self, bars, ids, source):
        written = 0
        for b in bars:
            if b.symbol in ids:
                self.raw_bars[(b.symbol, b.trade_date)] = b
                written += 1
        return written

    def upsert_adjusted_bars(self, bars, ids, source):
        return 0

    def upsert_corporate_actions(self, actions, ids, source):
        return len([a for a in actions if a.symbol in ids])

    def upsert_adjustment_factors(self, factors):
        for sid, d, f in factors:
            self.factors[(sid, d)] = f
        return len(factors)

    def mark_ingestion(self, source, data_date, status, rows, run_id):
        self.marks.append((source, data_date, status, rows))
        if status in ("loaded", "no_data"):
            self._settled.setdefault(source, set()).add(data_date)
        else:
            self._settled.get(source, set()).discard(data_date)

    def settled_dates(self, source):
        return set(self._settled.get(source, set()))

    def last_loaded_date(self, source):
        dates = self._settled.get(source, set())
        return max(dates) if dates else None

    def record_findings(self, run_id, findings):
        self.findings.extend(findings)
        return len(findings)

    def paired_closes(self, trade_date):
        return self.paired.get(trade_date, [])


class FakeNse:
    name = "nse"

    def __init__(self, bars_by_date=None, universe=None, fail_dates=(), actions=()):
        self.bars_by_date = bars_by_date or {}
        self.universe = universe or [FakeUniverseRow("A", "INE0A"), FakeUniverseRow("B", "INE0B")]
        self.fail_dates = set(fail_dates)
        self.actions = list(actions)
        self.fetched = []

    def fetch_universe(self):
        return self.universe

    def fetch_daily_bars(self, trade_date):
        self.fetched.append(trade_date)
        if trade_date in self.fail_dates:
            raise RuntimeError("simulated fetch failure")
        return self.bars_by_date.get(trade_date)

    def fetch_corporate_actions(self):
        return self.actions


def bars_for(d):
    return [FakeBar("A", d), FakeBar("B", d)]


class TestOutstandingDates:
    def test_lists_unsettled_weekdays(self):
        repo = FakeRepo()
        pending = outstanding_dates(repo, NSE_SOURCE, date(2026, 9, 22), catchup_days=5)
        assert date(2026, 9, 21) in pending
        assert date(2026, 9, 19) not in pending, "Saturday must be excluded"

    def test_excludes_today(self):
        """The archive is published after the close; fetching today races it."""
        repo = FakeRepo()
        pending = outstanding_dates(repo, NSE_SOURCE, date(2026, 9, 22), catchup_days=5)
        assert date(2026, 9, 22) not in pending

    def test_excludes_already_settled_dates(self):
        repo = FakeRepo({NSE_SOURCE: {date(2026, 9, 21)}})
        pending = outstanding_dates(repo, NSE_SOURCE, date(2026, 9, 22), catchup_days=5)
        assert date(2026, 9, 21) not in pending

    def test_catches_up_after_a_gap(self):
        """A skipped scheduler run must self-heal, not need manual repair."""
        repo = FakeRepo()
        pending = outstanding_dates(repo, NSE_SOURCE, date(2026, 9, 22), catchup_days=10)
        assert len(pending) >= 5


class TestPipelineRun:
    def test_loads_outstanding_dates(self):
        d = date(2026, 9, 21)
        repo = FakeRepo()
        nse = FakeNse({d: bars_for(d)})
        summary = DailyPipeline(repo, nse=nse).run(today=date(2026, 9, 22), catchup_days=3)
        assert d in summary.dates_loaded
        assert summary.raw_bars_written == 2

    def test_universe_is_archived_before_bars(self):
        """Survivorship data cannot be reconstructed, so it must not be
        blocked by a later step."""
        d = date(2026, 9, 21)
        repo = FakeRepo()
        summary = DailyPipeline(repo, nse=FakeNse({d: bars_for(d)})).run(
            today=date(2026, 9, 22), catchup_days=3)
        assert summary.universe_symbols == 2
        assert repo.universe[date(2026, 9, 22)] == ["INE0A", "INE0B"]

    def test_rerun_is_idempotent(self):
        """A second run on the same day must do no duplicate work."""
        d = date(2026, 9, 21)
        repo = FakeRepo()
        nse = FakeNse({d: bars_for(d)})
        pipeline = DailyPipeline(repo, nse=nse)
        pipeline.run(today=date(2026, 9, 22), catchup_days=3)
        first_fetches = len(nse.fetched)
        second = pipeline.run(today=date(2026, 9, 22), catchup_days=3)
        assert second.dates_loaded == []
        assert len(nse.fetched) == first_fetches, "settled dates must not be refetched"

    def test_holiday_is_recorded_as_no_data_and_not_refetched(self):
        repo = FakeRepo()
        nse = FakeNse({})  # every date returns None
        pipeline = DailyPipeline(repo, nse=nse)
        first = pipeline.run(today=date(2026, 9, 22), catchup_days=3)
        assert first.dates_no_data
        count = len(nse.fetched)
        pipeline.run(today=date(2026, 9, 22), catchup_days=3)
        assert len(nse.fetched) == count, "holidays must be learned once"

    def test_failed_date_is_retried_on_the_next_run(self):
        d = date(2026, 9, 21)
        repo = FakeRepo()
        nse = FakeNse({d: bars_for(d)}, fail_dates=[d])
        pipeline = DailyPipeline(repo, nse=nse)
        first = pipeline.run(today=date(2026, 9, 22), catchup_days=3)
        assert d in first.dates_failed

        nse.fail_dates.clear()
        second = pipeline.run(today=date(2026, 9, 22), catchup_days=3)
        assert d in second.dates_loaded

    def test_one_bad_date_does_not_abort_the_run(self):
        good, bad = date(2026, 9, 21), date(2026, 9, 18)
        repo = FakeRepo()
        nse = FakeNse({good: bars_for(good), bad: bars_for(bad)}, fail_dates=[bad])
        summary = DailyPipeline(repo, nse=nse).run(today=date(2026, 9, 22), catchup_days=10)
        assert good in summary.dates_loaded
        assert bad in summary.dates_failed

    def test_integrity_error_rejects_the_date(self):
        d = date(2026, 9, 21)
        dupes = [FakeBar("A", d), FakeBar("A", d)]
        repo = FakeRepo()
        summary = DailyPipeline(repo, nse=FakeNse({d: dupes})).run(
            today=date(2026, 9, 22), catchup_days=3)
        assert d in summary.dates_failed
        assert d not in summary.dates_loaded
        assert summary.halted, "a wrong report is worse than no report"

    def test_halted_run_is_recorded_as_halted(self):
        d = date(2026, 9, 21)
        repo = FakeRepo()
        DailyPipeline(repo, nse=FakeNse({d: [FakeBar("A", d), FakeBar("A", d)]})).run(
            today=date(2026, 9, 22), catchup_days=3)
        assert repo.runs[-1]["status"] == "halted"

    def test_clean_run_is_recorded_as_succeeded(self):
        d = date(2026, 9, 21)
        repo = FakeRepo()
        DailyPipeline(repo, nse=FakeNse({d: bars_for(d)})).run(
            today=date(2026, 9, 22), catchup_days=3)
        assert repo.runs[-1]["status"] == "succeeded"

    def test_fetch_exception_is_recorded_then_reraised_for_the_run(self):
        class ExplodingNse(FakeNse):
            def fetch_universe(self):
                raise RuntimeError("boom")

        repo = FakeRepo()
        with pytest.raises(RuntimeError):
            DailyPipeline(repo, nse=ExplodingNse()).run(today=date(2026, 9, 22))
        assert repo.runs[-1]["status"] == "failed"


class TestCrossSourceIntegrationInPipeline:
    def test_adjustment_factors_are_persisted_for_loaded_dates(self):
        d = date(2026, 9, 21)
        repo = FakeRepo()
        # Real Phase 0 numbers: raw 2655.70 vs adjusted 1327.85 => factor 2.0
        repo.paired[d] = [(1, 2655.70, 1327.85, 9_298_748, 18_597_496)]
        summary = DailyPipeline(repo, nse=FakeNse({d: bars_for(d)})).run(
            today=date(2026, 9, 22), catchup_days=3)
        assert summary.factors_written == 1
        assert repo.factors[(1, d)] == pytest.approx(2.0)

    def test_implausible_pair_halts_the_run(self):
        d = date(2026, 9, 21)
        repo = FakeRepo()
        repo.paired[d] = [(1, 100000.0, 20.0, 100, 100)]
        summary = DailyPipeline(repo, nse=FakeNse({d: bars_for(d)})).run(
            today=date(2026, 9, 22), catchup_days=3)
        assert summary.halted
