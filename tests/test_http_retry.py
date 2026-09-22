"""Retry/backoff behaviour.

Phase 0 addendum A.3: NSE archives return HTTP 403 intermittently and
recover. A loader treating one 403 as an outage would fail randomly.
"""
import urllib.error

import pytest

from src.http_client import FetchError, HttpClient


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_opener(outcomes):
    """Opener yielding each outcome in turn: an int raises that HTTP status."""
    calls = {"n": 0}

    def opener(req, timeout=None):
        index = calls["n"]
        calls["n"] += 1
        outcome = outcomes[min(index, len(outcomes) - 1)]
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(req.full_url, outcome, "err", {}, None)
        return FakeResponse(200, outcome)

    opener.calls = calls
    return opener


def client_with(opener, **kw):
    return HttpClient(sleeper=lambda _s: None, opener=opener, **kw)


class TestRetry:
    def test_succeeds_first_try(self):
        opener = make_opener([b"ok"])
        assert client_with(opener).get("https://x/a").body == b"ok"
        assert opener.calls["n"] == 1

    def test_recovers_from_transient_403(self):
        """The exact Phase 0 failure: 403 then success."""
        opener = make_opener([403, b"data"])
        resp = client_with(opener).get("https://x/a")
        assert resp.ok and resp.body == b"data"
        assert opener.calls["n"] == 2

    def test_recovers_after_several_403s(self):
        opener = make_opener([403, 403, 403, b"data"])
        assert client_with(opener).get("https://x/a").body == b"data"
        assert opener.calls["n"] == 4

    def test_raises_after_exhausting_retries(self):
        opener = make_opener([403])
        with pytest.raises(FetchError) as exc:
            client_with(opener, max_retries=2).get("https://x/a")
        assert exc.value.status == 403
        assert exc.value.attempts == 3
        assert opener.calls["n"] == 3

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_retries_other_transient_statuses(self, status):
        opener = make_opener([status, b"ok"])
        assert client_with(opener).get("https://x/a").ok
        assert opener.calls["n"] == 2

    def test_does_not_retry_404(self):
        """A missing bhavcopy is a holiday, not a transient failure."""
        opener = make_opener([404])
        with pytest.raises(FetchError) as exc:
            client_with(opener).get("https://x/a")
        assert exc.value.status == 404
        assert opener.calls["n"] == 1, "404 must not be retried"

    def test_does_not_retry_401(self):
        """An expired Upstox token must surface immediately, not retry."""
        opener = make_opener([401])
        with pytest.raises(FetchError) as exc:
            client_with(opener).get("https://x/a")
        assert exc.value.status == 401
        assert opener.calls["n"] == 1

    def test_retries_transport_errors(self):
        calls = {"n": 0}

        def opener(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionResetError("reset")
            return FakeResponse(200, b"ok")

        assert client_with(opener).get("https://x/a").body == b"ok"
        assert calls["n"] == 2

    def test_get_optional_returns_none_on_404(self):
        opener = make_opener([404])
        assert client_with(opener).get_optional("https://x/a") is None

    def test_get_optional_still_raises_on_persistent_403(self):
        opener = make_opener([403])
        with pytest.raises(FetchError):
            client_with(opener, max_retries=1).get_optional("https://x/a")


class TestBackoff:
    def test_backoff_is_exponential_without_jitter(self):
        c = HttpClient(backoff_base=5.0, jitter=0.0)
        assert [c.backoff_for(i) for i in range(5)] == [5.0, 10.0, 20.0, 40.0, 80.0]

    def test_jitter_stays_within_bounds(self):
        c = HttpClient(backoff_base=5.0, jitter=0.25)
        for attempt in range(4):
            base = 5.0 * (2**attempt)
            for _ in range(50):
                delay = c.backoff_for(attempt)
                assert base * 0.75 <= delay <= base * 1.25

    def test_jitter_actually_varies(self):
        c = HttpClient(backoff_base=5.0, jitter=0.25)
        assert len({c.backoff_for(0) for _ in range(20)}) > 1

    def test_floor_is_high_enough_to_outlast_the_observed_throttle(self):
        """Phase 1 measured NSE clearing in ~30s while a 2/4/8/16s ladder
        exhausted itself mid-block. The total budget must comfortably exceed
        that window."""
        c = HttpClient(backoff_base=5.0, jitter=0.0)
        total = sum(c.backoff_for(i) for i in range(5))
        assert total >= 120, f"retry budget {total}s is too short"

    def test_sleeps_between_attempts(self):
        slept = []
        opener = make_opener([403, 403, b"ok"])
        HttpClient(sleeper=slept.append, opener=opener, jitter=0.0, backoff_base=5.0).get(
            "https://x/a"
        )
        assert slept == [5.0, 10.0]

    def test_no_sleep_on_success(self):
        slept = []
        HttpClient(sleeper=slept.append, opener=make_opener([b"ok"])).get("https://x/a")
        assert slept == []


class TestReadOnly:
    def test_client_exposes_no_write_verbs(self):
        """The system is read-only by construction (report section 7)."""
        for verb in ("post", "put", "delete", "patch"):
            assert not hasattr(HttpClient, verb), f"HttpClient must not expose {verb}"
