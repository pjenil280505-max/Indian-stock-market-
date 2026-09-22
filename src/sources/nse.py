"""NSE official archive adapter - the primary data source.

Verified working in Phase 0 with no authentication:
  - BhavCopy_NSE_CM_*.csv.zip   raw OHLCV for the whole market, ~206 KB/day
  - sec_bhavdata_full_*.csv     adds DELIV_QTY / DELIV_PER (delivery volume)
  - EQUITY_L.csv                the listed-equity universe
  - corporates-corporateActions ex-dates and purposes

Prices here are RAW / as-traded. They are NOT adjusted for corporate actions
(proven in docs/PHASE_0_REPORT.md section 2.4). Adjusted series come from
the Upstox adapter, and the two are cross-checked in src/integrity.py.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
import zipfile
from dataclasses import dataclass
from datetime import date

from ..config import NSE_API, NSE_ARCHIVES, NSE_MIN_INTERVAL_SECONDS
from ..http_client import HttpClient

log = logging.getLogger(__name__)

# Equity series we treat as in scope. EQ is the ordinary rolling-settlement
# series. BE/BZ carry price bands and different liquidity; they are archived
# in the universe for completeness but flagged, and whether to make them
# eligible is deliberately left to Phase 2 (report question U6).
EQUITY_SERIES = ("EQ", "BE", "BZ")


@dataclass(frozen=True)
class UniverseRow:
    symbol: str
    name: str
    series: str
    listing_date: date | None
    isin: str
    face_value: float | None


@dataclass(frozen=True)
class RawBar:
    """One as-traded daily bar. Prices unadjusted."""

    symbol: str
    isin: str | None
    series: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    prev_close: float | None
    last_price: float | None
    volume: int
    turnover: float | None
    trades: int | None
    deliv_qty: int | None
    deliv_pct: float | None


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    isin: str | None
    ex_date: date
    purpose: str
    face_value: float | None


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _to_float(value: str | None) -> float | None:
    text = _clean(value).replace(",", "")
    if text in ("", "-", "NA", "N/A"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: str | None) -> int | None:
    number = _to_float(value)
    return None if number is None else int(number)


def _row_get(row: dict[str, str], name: str) -> str | None:
    """Fetch a column tolerating NSE's inconsistent leading spaces in headers."""
    if name in row:
        return row[name]
    for key in row:
        if key is not None and key.strip() == name:
            return row[key]
    return None


def parse_equity_list(body: bytes) -> list[UniverseRow]:
    """Parse EQUITY_L.csv.

    Real header: 'SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP
    VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE' - note the leading spaces.
    """
    rows: list[UniverseRow] = []
    reader = csv.DictReader(io.StringIO(body.decode("utf-8", "replace")))
    for row in reader:
        isin = _clean(_row_get(row, "ISIN NUMBER"))
        symbol = _clean(_row_get(row, "SYMBOL"))
        if not isin or not symbol:
            continue
        listing = _clean(_row_get(row, "DATE OF LISTING"))
        rows.append(
            UniverseRow(
                symbol=symbol,
                name=_clean(_row_get(row, "NAME OF COMPANY")),
                series=_clean(_row_get(row, "SERIES")),
                listing_date=_parse_listing_date(listing),
                isin=isin,
                face_value=_to_float(_row_get(row, "FACE VALUE")),
            )
        )
    return rows


def _parse_listing_date(text: str) -> date | None:
    """EQUITY_L uses DD-MON-YYYY, e.g. 06-OCT-2008."""
    from datetime import datetime

    if not text:
        return None
    try:
        return datetime.strptime(text.upper(), "%d-%b-%Y").date()
    except ValueError:
        return None


def parse_delivery_bhavcopy(body: bytes, trade_date: date) -> list[RawBar]:
    """Parse sec_bhavdata_full_DDMMYYYY.csv.

    This file is preferred over the plain bhavcopy because it carries
    DELIV_QTY and DELIV_PER - delivery volume, available from NSE and from no
    broker API tested in Phase 0.
    """
    bars: list[RawBar] = []
    reader = csv.DictReader(io.StringIO(body.decode("utf-8", "replace")))
    for row in reader:
        series = _clean(_row_get(row, "SERIES"))
        symbol = _clean(_row_get(row, "SYMBOL"))
        if not symbol or series not in EQUITY_SERIES:
            continue
        close = _to_float(_row_get(row, "CLOSE_PRICE"))
        open_ = _to_float(_row_get(row, "OPEN_PRICE"))
        high = _to_float(_row_get(row, "HIGH_PRICE"))
        low = _to_float(_row_get(row, "LOW_PRICE"))
        volume = _to_int(_row_get(row, "TTL_TRD_QNTY"))
        if None in (open_, high, low, close) or volume is None:
            continue
        turnover_lacs = _to_float(_row_get(row, "TURNOVER_LACS"))
        bars.append(
            RawBar(
                symbol=symbol,
                isin=None,  # this file carries no ISIN; resolved via the universe
                series=series,
                trade_date=trade_date,
                open=open_,
                high=high,
                low=low,
                close=close,
                prev_close=_to_float(_row_get(row, "PREV_CLOSE")),
                last_price=_to_float(_row_get(row, "LAST_PRICE")),
                volume=volume,
                # TURNOVER_LACS is in lakhs of rupees; store rupees.
                turnover=None if turnover_lacs is None else turnover_lacs * 100_000,
                trades=_to_int(_row_get(row, "NO_OF_TRADES")),
                deliv_qty=_to_int(_row_get(row, "DELIV_QTY")),
                deliv_pct=_to_float(_row_get(row, "DELIV_PER")),
            )
        )
    return bars


def parse_udiff_bhavcopy(zip_body: bytes, trade_date: date) -> list[RawBar]:
    """Parse the UDiFF BhavCopy zip. Used as a fallback; carries ISIN."""
    bars: list[RawBar] = []
    with zipfile.ZipFile(io.BytesIO(zip_body)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if not names:
            return bars
        with archive.open(names[0]) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8", errors="replace")
            for row in csv.DictReader(text):
                if _clean(_row_get(row, "FinInstrmTp")) != "STK":
                    continue
                series = _clean(_row_get(row, "SctySrs"))
                symbol = _clean(_row_get(row, "TckrSymb"))
                if not symbol or series not in EQUITY_SERIES:
                    continue
                open_ = _to_float(_row_get(row, "OpnPric"))
                high = _to_float(_row_get(row, "HghPric"))
                low = _to_float(_row_get(row, "LwPric"))
                close = _to_float(_row_get(row, "ClsPric"))
                volume = _to_int(_row_get(row, "TtlTradgVol"))
                if None in (open_, high, low, close) or volume is None:
                    continue
                bars.append(
                    RawBar(
                        symbol=symbol,
                        isin=_clean(_row_get(row, "ISIN")) or None,
                        series=series,
                        trade_date=trade_date,
                        open=open_,
                        high=high,
                        low=low,
                        close=close,
                        prev_close=_to_float(_row_get(row, "PrvsClsgPric")),
                        last_price=_to_float(_row_get(row, "LastPric")),
                        volume=volume,
                        turnover=_to_float(_row_get(row, "TtlTrfVal")),
                        trades=_to_int(_row_get(row, "TtlNbOfTxsExctd")),
                        deliv_qty=None,
                        deliv_pct=None,
                    )
                )
    return bars


def parse_corporate_actions(body: bytes) -> list[CorporateAction]:
    """Parse the corporates-corporateActions JSON response."""
    from datetime import datetime

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []

    actions: list[CorporateAction] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = _clean(item.get("symbol") or item.get("comp"))
        ex_text = _clean(item.get("exDate"))
        if not symbol or not ex_text or ex_text == "-":
            continue
        try:
            ex_date = datetime.strptime(ex_text.upper(), "%d-%b-%Y").date()
        except ValueError:
            continue
        actions.append(
            CorporateAction(
                symbol=symbol,
                isin=_clean(item.get("isin")) or None,
                ex_date=ex_date,
                purpose=_clean(item.get("subject") or item.get("purpose")) or "unspecified",
                face_value=_to_float(str(item.get("faceVal")) if item.get("faceVal") else None),
            )
        )
    return actions


class NseSource:
    """Fetches NSE official archive files. Read-only."""

    name = "nse"

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        min_interval: float = NSE_MIN_INTERVAL_SECONDS,
        sleeper=time.sleep,
        clock=time.monotonic,
    ):
        self.client = client or HttpClient()
        self.min_interval = min_interval
        self._sleep = sleeper
        self._clock = clock
        self._last_call = 0.0

    def _pace(self) -> None:
        """Keep a courtesy gap between archive requests.

        One file per day is proportionate; bursting is what draws the
        throttle (docs/PHASE_0_REPORT.md section 11).
        """
        if self._last_call:
            elapsed = self._clock() - self._last_call
            if elapsed < self.min_interval:
                self._sleep(self.min_interval - elapsed)
        self._last_call = self._clock()

    def fetch_universe(self) -> list[UniverseRow]:
        self._pace()
        resp = self.client.get(f"{NSE_ARCHIVES}/content/equities/EQUITY_L.csv")
        return parse_equity_list(resp.body)

    def fetch_daily_bars(self, trade_date: date) -> list[RawBar] | None:
        """Bars for one date, or None if the date was not a trading day.

        Tries the delivery file first (it carries DELIV_PER), then falls back
        to the UDiFF bhavcopy. A 404 from both means a market holiday.
        """
        stamp = trade_date.strftime("%d%m%Y")
        self._pace()
        resp = self.client.get_optional(
            f"{NSE_ARCHIVES}/products/content/sec_bhavdata_full_{stamp}.csv"
        )
        if resp is not None and resp.ok:
            bars = parse_delivery_bhavcopy(resp.body, trade_date)
            if bars:
                return bars
            log.warning("delivery file for %s parsed to 0 bars; trying UDiFF", trade_date)

        udiff_stamp = trade_date.strftime("%Y%m%d")
        self._pace()
        resp = self.client.get_optional(
            f"{NSE_ARCHIVES}/content/cm/BhavCopy_NSE_CM_0_0_0_{udiff_stamp}_F_0000.csv.zip"
        )
        if resp is None or not resp.ok:
            return None
        return parse_udiff_bhavcopy(resp.body, trade_date) or None

    def fetch_corporate_actions(self) -> list[CorporateAction]:
        """Forward-looking corporate actions. Best-effort: this endpoint is
        less reliable than the archive files, so a failure is not fatal."""
        self._pace()
        try:
            resp = self.client.get(f"{NSE_API}/corporates-corporateActions?index=equities")
        except Exception as exc:  # noqa: BLE001 - best effort by design
            log.warning("corporate actions unavailable: %s", exc)
            return []
        return parse_corporate_actions(resp.body)
