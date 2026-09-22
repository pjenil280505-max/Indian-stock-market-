"""Upstox Analytics Token adapter - the secondary source (adjusted history).

Uses the Analytics Token documented at
https://upstox.com/developer/api-documentation/analytics-token/ :

  - read-only, "only GET APIs are supported"
  - 1-year validity, so no daily OAuth/TOTP login
  - "does not support trading operations"; orders are not permitted
  - Historical Data needs NO static IP: callable "from any server, laptop or
    serverless function" - which is what makes GitHub Actions viable
  - free to use

Prices from this source are corporate-action BACK-ADJUSTED. Both prices and
volume are scaled (proven in docs/PHASE_0_REPORT.md section 2.4), so adjusted
volume is inflated by past corporate actions and must never be used for
liquidity filtering.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta

from ..config import UPSTOX_BASE, UPSTOX_MAX_WINDOW_DAYS, UPSTOX_MIN_INTERVAL_SECONDS
from ..http_client import FetchError, HttpClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdjustedBar:
    isin: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


def instrument_key(isin: str, exchange: str = "NSE_EQ") -> str:
    """Upstox instrument key, e.g. 'NSE_EQ|INE002A01018'."""
    return f"{exchange}|{isin}"


def parse_candles(body: bytes, isin: str) -> list[AdjustedBar]:
    """Parse a v3 historical-candle response.

    Candle shape: [timestamp, open, high, low, close, volume, open_interest].
    Upstox returns newest-first; we return oldest-first.
    """
    from datetime import datetime

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    candles = (payload.get("data") or {}).get("candles") or []

    bars: list[AdjustedBar] = []
    for candle in candles:
        if not isinstance(candle, (list, tuple)) or len(candle) < 6:
            continue
        try:
            stamp = datetime.fromisoformat(candle[0]).date()
            bars.append(
                AdjustedBar(
                    isin=isin,
                    trade_date=stamp,
                    open=float(candle[1]),
                    high=float(candle[2]),
                    low=float(candle[3]),
                    close=float(candle[4]),
                    volume=int(candle[5]),
                )
            )
        except (ValueError, TypeError):
            continue
    bars.sort(key=lambda bar: bar.trade_date)
    return bars


def split_window(start: date, end: date, max_days: int = UPSTOX_MAX_WINDOW_DAYS):
    """Split a date range into request-sized chunks, oldest first.

    A >5-year window for days/1 is rejected with UDAPI1148 'Invalid date
    range' (Phase 0 check 11), so long backfills must be chunked.
    """
    if start > end:
        return []
    chunks = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max_days), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


class UpstoxSource:
    """Fetches corporate-action adjusted daily candles. Read-only.

    The Analytics Token cannot place orders, so this adapter is incapable of
    trading by construction, not merely by omission.
    """

    name = "upstox"

    def __init__(
        self,
        token: str | None = None,
        client: HttpClient | None = None,
        *,
        min_interval: float = UPSTOX_MIN_INTERVAL_SECONDS,
        sleeper=time.sleep,
        clock=time.monotonic,
    ):
        self.token = token
        self.client = client or HttpClient()
        self.min_interval = min_interval
        self._sleep = sleeper
        self._clock = clock
        self._last_call = 0.0

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _throttle(self) -> None:
        """Stay well inside the documented 50 req/s limit."""
        elapsed = self._clock() - self._last_call
        if self._last_call and elapsed < self.min_interval:
            self._sleep(self.min_interval - elapsed)
        self._last_call = self._clock()

    def fetch_daily(self, isin: str, start: date, end: date) -> list[AdjustedBar]:
        """Adjusted daily bars for one instrument, chunked to fit the window cap."""
        bars: list[AdjustedBar] = []
        key = instrument_key(isin).replace("|", "%7C")
        for chunk_start, chunk_end in split_window(start, end):
            url = (
                f"{UPSTOX_BASE}/v3/historical-candle/{key}/days/1/"
                f"{chunk_end.isoformat()}/{chunk_start.isoformat()}"
            )
            self._throttle()
            try:
                resp = self.client.get(url, headers=self._headers())
            except FetchError as exc:
                if exc.status == 401:
                    raise  # token expired or missing - must alert, never degrade silently
                log.warning("upstox %s %s..%s failed: %s", isin, chunk_start, chunk_end, exc)
                continue
            bars.extend(parse_candles(resp.body, isin))
        return bars
