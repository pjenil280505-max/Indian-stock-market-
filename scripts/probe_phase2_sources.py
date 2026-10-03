#!/usr/bin/env python3
"""Phase 2.0 source probe. READ-ONLY, no database, no secrets.

    python3 scripts/probe_phase2_sources.py

Measures what Phase 2 can actually rely on, instead of assuming it:

  * do NSE's announcement, financial-result, shareholding and corporate-action
    endpoints answer a plain, honestly identified client (no cookie priming,
    no browser spoofing - a refusal is recorded, never evaded)?
  * which fields do they return, and is there an exchange timestamp?
  * how far back does each go (sample windows across the years)?
  * how many announcements per day (the storage budget)?
  * what do the linked XBRL files look like (size, tags, units, DOCTYPE)?

Prints metadata only - field names, counts, sizes, timestamp formats - never
bulk records, headlines or filing contents (NSE data policy; the repository
and its logs are public). About 25 requests, paced, single-threaded. If NSE
refuses the first API calls outright, the rest are skipped.

Exit code is always 0 unless the script itself breaks: a refusal is a
finding, not a failure.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import NSE_API  # noqa: E402

PACE_SECONDS = 2.0
MAX_XBRL_BYTES = 20 * 1024 * 1024
STOP_AFTER_REFUSALS = 3
TIMESTAMPISH = re.compile(r"(date|time|dt|dis|broad|filing|sort)", re.I)
# Reg 33 / shareholding element names worth knowing about (local names).
XBRL_TARGETS = (
    "RevenueFromOperations", "OtherIncome", "Income", "Expenses", "FinanceCosts",
    "DepreciationDepletionAndAmortisationExpense", "ProfitBeforeTax", "TaxExpense",
    "ProfitLossForPeriod", "BasicEarningsLossPerShareFromContinuingOperations",
    "DilutedEarningsLossPerShareFromContinuingOperations", "Assets", "Equity",
    "Borrowings", "CashAndCashEquivalents", "NatureOfReportStandaloneConsolidated",
    "ISIN", "Symbol", "ScripCode", "DateOfStartOfReportingPeriod", "DateOfEndOfReportingPeriod",
    "WhetherResultsAreAuditedOrUnaudited", "LevelOfRoundingUsedInFinancialStatements",
    "ShareholdingOfPromoterAndPromoterGroup", "NumberOfSharesPledgedOrOtherwiseEncumbered",
)


# ---- pure helpers (unit-tested offline) ------------------------------------

def nse_date(d: date) -> str:
    """NSE's query format, DD-MM-YYYY."""
    return d.strftime("%d-%m-%Y")


def records_of(payload) -> list[dict]:
    """NSE answers either a list or {'data': [...]}; anything else is empty."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "Data", "rows"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
    return []


def field_summary(records: list[dict]) -> dict:
    """Field names with how often each is filled, plus example timestamp values.

    Only timestamp-like fields have their values shown; every other value is
    reduced to its type, so no headline or filing text is ever printed.
    """
    filled: Counter = Counter()
    kinds: dict[str, str] = {}
    stamps: dict[str, str] = {}
    for r in records:
        for k, v in r.items():
            if v not in (None, "", "-"):
                filled[k] += 1
                kinds.setdefault(k, type(v).__name__)
                if TIMESTAMPISH.search(k) and isinstance(v, (str, int)) and k not in stamps:
                    stamps[k] = str(v)[:40]
    return {
        "fields": {k: f"{kinds.get(k, '?')} {filled[k]}/{len(records)}" for k in sorted({*filled, *(k for r in records for k in r)})},
        "timestamp_examples": stamps,
    }


def xml_links(records: list[dict]) -> list[str]:
    """Every value that looks like a link to an XBRL/XML file."""
    out = []
    for r in records:
        for v in r.values():
            if isinstance(v, str) and v.lower().startswith("http") and v.lower().endswith(".xml"):
                out.append(v)
    return out


def inspect_xbrl(body: bytes) -> dict:
    """Structure of one XBRL instance, without trusting it.

    Refuses DOCTYPE/ENTITY (entity-expansion attacks) and oversized files
    before parsing. Reports names and counts only, plus the ISIN (a public
    identifier) so issuer mapping can be checked.
    """
    if len(body) > MAX_XBRL_BYTES:
        return {"rejected": f"larger than {MAX_XBRL_BYTES} bytes"}
    if re.search(rb"<!DOCTYPE|<!ENTITY", body[:MAX_XBRL_BYTES], re.I):
        return {"rejected": "contains DOCTYPE/ENTITY"}
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        return {"rejected": f"not well-formed XML ({exc})"}
    local = lambda tag: tag.rsplit("}", 1)[-1]  # noqa: E731
    names: Counter = Counter()
    units: Counter = Counter()
    decimals: Counter = Counter()
    contexts = 0
    isin = None
    for el in root.iter():
        name = local(el.tag)
        names[name] += 1
        if name == "context":
            contexts += 1
        if "unitRef" in el.attrib:
            units[el.attrib["unitRef"]] += 1
        if "decimals" in el.attrib:
            decimals[el.attrib["decimals"]] += 1
        if name == "ISIN" and el.text and not isin:
            isin = el.text.strip()[:12]
    namespaces = sorted(set(re.findall(rb'xmlns:([A-Za-z0-9_-]+)=', body[:20000])))
    return {
        "bytes": len(body),
        "root": local(root.tag),
        "namespace_prefixes": [n.decode() for n in namespaces][:25],
        "elements": sum(names.values()),
        "distinct_elements": len(names),
        "contexts": contexts,
        "units": dict(units.most_common(6)),
        "decimals": dict(decimals.most_common(6)),
        "isin": isin,
        "targets_present": [t for t in XBRL_TARGETS if t in names],
        "targets_missing": [t for t in XBRL_TARGETS if t not in names],
    }


# ---- network -----------------------------------------------------------------

class Probe:
    def __init__(self):
        from src.http_client import HttpClient

        self.headers: dict[str, str] = {}

        def opener(req, timeout):  # keep response headers for the report
            resp = urllib.request.urlopen(req, timeout=timeout)
            self.headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp

        # One retry only: a hard refusal must cost seconds, not minutes.
        self.client = HttpClient(max_retries=1, opener=opener)
        self.refusals = 0
        self.successes = 0
        self.requests = 0

    def get(self, url: str) -> tuple[int, bytes, dict]:
        from src.http_client import FetchError

        time.sleep(PACE_SECONDS)
        self.requests += 1
        self.headers = {}
        try:
            resp = self.client.get(url, headers={"Accept": "application/json, */*"})
            self.successes += 1
            return resp.status, resp.body, self.headers
        except FetchError as exc:
            if exc.status in (401, 403):
                self.refusals += 1
            return exc.status, b"", {}

    @property
    def refused(self) -> bool:
        return self.successes == 0 and self.refusals >= STOP_AFTER_REFUSALS

    def api(self, label: str, path: str) -> list[dict]:
        if self.refused:
            print(f"\n[{label}] SKIPPED: NSE refused the first {self.refusals} API calls; not pressing further")
            return []
        status, body, headers = self.get(f"{NSE_API}/{path}")
        ctype = headers.get("content-type", "")
        print(f"\n[{label}] GET /api/{path.split('?')[0]} -> {status} {ctype[:40]} {len(body):,} bytes")
        if status != 200:
            return []
        try:
            recs = records_of(json.loads(body))
        except ValueError:
            print(f"  not JSON; starts: {body[:60]!r}")
            return []
        print(f"  records: {len(recs):,}; avg bytes/record: {len(body) // max(1, len(recs)):,}")
        if recs:
            s = field_summary(recs)
            print(f"  fields: {json.dumps(s['fields'])}")
            print(f"  timestamp examples: {json.dumps(s['timestamp_examples'])}")
        return recs

    def xbrl(self, label: str, url: str) -> None:
        if self.refused:
            return
        status, body, headers = self.get(url)
        host = url.split("/")[2]
        print(f"\n[{label}] XBRL {host} -> {status} {headers.get('content-type', '')[:30]}"
              f" last-modified: {headers.get('last-modified', 'not sent')}")
        if status == 200:
            print(f"  {json.dumps(inspect_xbrl(body))}")


def main() -> int:
    p = Probe()
    print("Phase 2.0 source probe - metadata only; nothing stored, no database")

    # 1. Announcements: per-day volume now, then depth across the years.
    ann = {}
    for d in (date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)):
        ann[d] = p.api(f"announcements {d}", f"corporate-announcements?index=equities&from_date={nse_date(d)}&to_date={nse_date(d)}")
    for d in (date(2025, 10, 1), date(2021, 10, 1), date(2016, 10, 3), date(2011, 10, 3), date(2008, 10, 1)):
        p.api(f"announcements depth {d}", f"corporate-announcements?index=equities&from_date={nse_date(d)}&to_date={nse_date(d)}")

    # 2. Financial results (Reg 33) in results-season weeks across the years.
    results = {}
    for start, end in ((date(2026, 8, 7), date(2026, 8, 14)), (date(2021, 8, 6), date(2021, 8, 13)),
                       (date(2016, 8, 8), date(2016, 8, 12)), (date(2012, 8, 6), date(2012, 8, 10))):
        results[start.year] = p.api(
            f"results {start}..{end}",
            f"corporates-financial-results?index=equities&period=Quarterly&from_date={nse_date(start)}&to_date={nse_date(end)}")

    # 3. Shareholding patterns (due 21 days after quarter end).
    shp = {}
    for start, end in ((date(2026, 7, 15), date(2026, 7, 21)), (date(2021, 7, 15), date(2021, 7, 21)),
                       (date(2016, 7, 15), date(2016, 7, 21)), (date(2012, 7, 16), date(2012, 7, 20))):
        shp[start.year] = p.api(
            f"shareholding {start}..{end}",
            f"corporate-share-holdings-master?index=equities&from_date={nse_date(start)}&to_date={nse_date(end)}")

    # 4. Corporate actions with a date range (open question U8: is there history?).
    for start, end in ((date(2026, 9, 1), date(2026, 9, 30)), (date(2021, 9, 1), date(2021, 9, 30)),
                       (date(2016, 9, 1), date(2016, 9, 30))):
        p.api(f"corporate actions {start}..{end}",
              f"corporates-corporateActions?index=equities&from_date={nse_date(start)}&to_date={nse_date(end)}")

    # 5. The XBRL documents themselves: newest and oldest results, newest shareholding.
    years = sorted(y for y, recs in results.items() if xml_links(recs))
    if years:
        p.xbrl(f"results XBRL {years[-1]}", xml_links(results[years[-1]])[0])
        if years[0] != years[-1]:
            p.xbrl(f"results XBRL {years[0]}", xml_links(results[years[0]])[0])
    print(f"\nresults years with XBRL links: {years or 'none found'}")
    shp_years = sorted(y for y, recs in shp.items() if xml_links(recs))
    if shp_years:
        p.xbrl(f"shareholding XBRL {shp_years[-1]}", xml_links(shp[shp_years[-1]])[0])
    print(f"shareholding years with XBRL links: {shp_years or 'none found'}")

    per_day = [len(v) for v in ann.values() if v]
    print(f"\nSUMMARY requests={p.requests} ok={p.successes} refused={p.refusals}"
          f" announcements/day sample={per_day or 'n/a'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
