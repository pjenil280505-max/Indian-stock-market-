"""Configuration, sourced from environment variables only.

No secret is ever read from a file in the repository. See
docs/PHASE_0_REPORT.md section 12.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Identify the client honestly. Upstox rejects the default "Python-urllib/x.y"
# User-Agent with HTTP 403 (Phase 0 check 13). Spoofing a browser string would
# also work but is the wrong posture toward a data provider.
USER_AGENT = "indian-stock-research/0.1 (+personal research; contact via repository owner)"

NSE_ARCHIVES = "https://nsearchives.nseindia.com"
NSE_API = "https://www.nseindia.com/api"
UPSTOX_BASE = "https://api.upstox.com"

# Phase 0 finding A.3: NSE archives throttle intermittently with HTTP 403,
# moving between paths and recovering on retry. Treat 403 as transient here.
RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
# Measured in Phase 1: NSE's throttle is short (it cleared within ~30s), but
# retrying rapidly RENEWS it - a 2/4/8/16s ladder exhausted itself while the
# block was still active, then the very next request 28s later succeeded.
# A higher floor plus jitter is both more robust and more courteous than
# more frequent retries.
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_JITTER = 0.25  # +/- fraction, so retries do not fall into lockstep
HTTP_TIMEOUT_SECONDS = 60
# Courtesy pause between consecutive NSE archive requests.
NSE_MIN_INTERVAL_SECONDS = 1.0

# Upstox documented limits: 50 req/s, 500 req/min, 2000 req/30min.
# We stay far below - the bhavcopy carries the whole market in one file, so
# Upstox is only used for adjusted history.
UPSTOX_MIN_INTERVAL_SECONDS = 0.25

# Upstox rejects a >5 year window for days/1 with UDAPI1148 (Phase 0 check 11).
UPSTOX_MAX_WINDOW_DAYS = 1800

# How recently a date must fall for a missing archive file to mean "not
# published yet" rather than "market holiday".
#
# This matters because the pipeline now attempts the CURRENT trading day. NSE
# publishes the bhavcopy within an hour or so of the close but the delivery
# file (sec_bhavdata_full) lands later, so an early run can legitimately find
# nothing. Marking that as a holiday would silently and permanently lose a
# trading day - the archive is never revisited once a date settles.
PUBLICATION_GRACE_DAYS = 3

# Minutes after the 15:30 IST close before the current day is worth attempting.
IST_CLOSE_MINUTES = 15 * 60 + 30
SAME_DAY_ATTEMPT_AFTER_MINUTES = 60


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    upstox_analytics_token: str | None
    commit_sha: str

    @property
    def has_database(self) -> bool:
        return bool(self.database_url)

    @property
    def has_upstox(self) -> bool:
        return bool(self.upstox_analytics_token)


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL"),
        upstox_analytics_token=os.environ.get("UPSTOX_ANALYTICS_TOKEN"),
        commit_sha=os.environ.get("GITHUB_SHA", "local"),
    )
