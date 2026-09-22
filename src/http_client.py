"""HTTP client with retry and exponential backoff.

Phase 0 addendum A.3 established that NSE archives return HTTP 403
intermittently: the same URL fails on one request and succeeds on the next,
and the failure moves between paths. It is per-client throttling that
recovers, not a block, and it is not User-Agent related. A loader that treats
a single 403 as a source outage will fail randomly.
"""
from __future__ import annotations

import logging
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_JITTER,
    HTTP_TIMEOUT_SECONDS,
    MAX_RETRIES,
    RETRY_STATUSES,
    USER_AGENT,
)

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """Raised when a URL could not be fetched after exhausting retries."""

    def __init__(self, url: str, status: int, attempts: int):
        self.url = url
        self.status = status
        self.attempts = attempts
        super().__init__(f"GET {url} failed with status {status} after {attempts} attempt(s)")


@dataclass
class Response:
    status: int
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpClient:
    """GET-only HTTP client.

    Deliberately exposes no POST/PUT/DELETE. The system is read-only by
    construction, not merely by convention (docs/PHASE_0_REPORT.md section 7).
    """

    def __init__(
        self,
        *,
        max_retries: int = MAX_RETRIES,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        timeout: int = HTTP_TIMEOUT_SECONDS,
        jitter: float = BACKOFF_JITTER,
        sleeper=time.sleep,
        opener=None,
        rng=None,
    ):
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
        self.jitter = jitter
        self._sleep = sleeper
        self._opener = opener or urllib.request.urlopen
        self._rng = rng or random.Random()

    def backoff_for(self, attempt: int) -> float:
        """Seconds before retry `attempt` (0-indexed): 5, 10, 20, 40, 80 +/- jitter.

        The jitter keeps concurrent clients out of lockstep. The floor matters
        more than the ceiling here: retrying too soon renews NSE's throttle
        rather than waiting it out.
        """
        base = self.backoff_base * (2**attempt)
        if not self.jitter:
            return base
        return base * (1.0 + self._rng.uniform(-self.jitter, self.jitter))

    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        """GET a URL, retrying transient failures. Raises FetchError if all fail."""
        request_headers = {"User-Agent": USER_AGENT}
        if headers:
            request_headers.update(headers)
        req = urllib.request.Request(url, headers=request_headers)

        last_status = 0
        for attempt in range(self.max_retries + 1):
            try:
                with self._opener(req, timeout=self.timeout) as resp:
                    return Response(resp.status, resp.read())
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                if last_status not in RETRY_STATUSES:
                    raise FetchError(url, last_status, attempt + 1) from exc
            except Exception:  # transport error: DNS, reset, timeout
                last_status = 0

            if attempt < self.max_retries:
                delay = self.backoff_for(attempt)
                log.warning(
                    "GET %s -> %s; retrying in %.1fs (attempt %d/%d)",
                    url, last_status or "transport error", delay, attempt + 1, self.max_retries,
                )
                self._sleep(delay)

        raise FetchError(url, last_status, self.max_retries + 1)

    def get_optional(self, url: str, headers: dict[str, str] | None = None) -> Response | None:
        """Like get(), but returns None on a definitive 404.

        A missing bhavcopy means the date was not a trading day, which is
        information rather than an error.
        """
        try:
            return self.get(url, headers)
        except FetchError as exc:
            if exc.status == 404:
                return None
            raise
