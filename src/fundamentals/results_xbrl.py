"""Financial-results XBRL parser (Phase 2.3). Pure: bytes in, typed facts out.

Handles the NSE results XBRL families measured in step 2.0:

  taxonomy in-bse-fin   classic results endpoint, 2018 - Feb 2025
  taxonomy in-capmkt    integrated filings, quarter ending Mar 2025 onwards
  templates             general Ind AS (incl. NBFC and non-Ind-AS) and banking,
                        which uses different element names

Rules, all deliberate:

  * Only NON-dimensional contexts are read (dimensional ones are segments
    and breakdowns, not company totals).
  * A context is used only if its dates match the filing's own reporting
    period: profit and loss from the reporting period, balance sheet from
    the instant at period end, cash flow from the year-to-date period.
    Comparative-period figures inside a filing are ignored - the earlier
    filing is the point-in-time source for those.
  * Values are kept exactly as filed (Decimal, in rupees as the XBRL states
    them); `decimals` is kept as the precision. Nothing is rescaled or
    guessed. A missing fact stays missing - never zero.
  * Units are checked: money must be INR, EPS INR per share, ratios pure.
  * Every rejection carries a reason code; consistency checks that can be
    legitimately off (rounding, discontinued operations) are warnings.

No database, no network, no AI.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

MAX_BYTES = 20 * 1024 * 1024
FLOW, STOCK, CASHFLOW = "flow", "stock", "cashflow"
INR, PER_SHARE, PURE = "INR", "INRPerShare", "pure"

# canonical field -> (candidate element names in priority order, kind, unit)
GENERAL = {
    "revenue": (["RevenueFromOperations"], FLOW, INR),
    "other_income": (["OtherIncome"], FLOW, INR),
    "total_income": (["Income"], FLOW, INR),
    "total_expenses": (["Expenses"], FLOW, INR),
    "finance_costs": (["FinanceCosts"], FLOW, INR),
    "depreciation": (["DepreciationDepletionAndAmortisationExpense"], FLOW, INR),
    "exceptional_items": (["ExceptionalItemsBeforeTax"], FLOW, INR),
    "pbt": (["ProfitBeforeTax"], FLOW, INR),
    "tax": (["TaxExpense"], FLOW, INR),
    "pat": (["ProfitLossForPeriod"], FLOW, INR),
    "pat_continuing": (["ProfitLossForPeriodFromContinuingOperations"], FLOW, INR),
    "eps_basic": (["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                   "BasicEarningsLossPerShareFromContinuingOperations"], FLOW, PER_SHARE),
    "eps_diluted": (["DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                     "DilutedEarningsLossPerShareFromContinuingOperations"], FLOW, PER_SHARE),
    "paid_up_capital": (["PaidUpValueOfEquityShareCapital"], FLOW, INR),
    "total_assets": (["Assets"], STOCK, INR),
    "total_liabilities": (["Liabilities"], STOCK, INR),
    "total_equity": (["Equity"], STOCK, INR),
    "equity_owners": (["EquityAttributableToOwnersOfParent"], STOCK, INR),
    "current_assets": (["CurrentAssets"], STOCK, INR),
    "current_liabilities": (["CurrentLiabilities"], STOCK, INR),
    "borrowings_current": (["BorrowingsCurrent"], STOCK, INR),
    "borrowings_noncurrent": (["BorrowingsNoncurrent"], STOCK, INR),
    "cash": (["CashAndCashEquivalents"], STOCK, INR),
    "cfo": (["CashFlowsFromUsedInOperatingActivities"], CASHFLOW, INR),
    "cfi": (["CashFlowsFromUsedInInvestingActivities"], CASHFLOW, INR),
    "cff": (["CashFlowsFromUsedInFinancingActivities"], CASHFLOW, INR),
    "capex": (["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"], CASHFLOW, INR),
}
BANKING = {
    "interest_earned": (["InterestEarned"], FLOW, INR),
    "other_income": (["OtherIncome"], FLOW, INR),
    "total_income": (["Income"], FLOW, INR),
    "interest_expended": (["InterestExpended"], FLOW, INR),
    "operating_expenses": (["OperatingExpenses"], FLOW, INR),
    "operating_profit_pre_provisions": (["OperatingProfitBeforeProvisionAndContingencies"], FLOW, INR),
    "provisions": (["ProvisionsOtherThanTaxAndContingencies"], FLOW, INR),
    "exceptional_items": (["ExceptionalItems"], FLOW, INR),
    "pbt": (["ProfitLossFromOrdinaryActivitiesBeforeTax"], FLOW, INR),
    "tax": (["TaxExpense"], FLOW, INR),
    "pat": (["ProfitLossForThePeriod"], FLOW, INR),
    "eps_basic": (["BasicEarningsPerShareBeforeExtraordinaryItems"], FLOW, PER_SHARE),
    "eps_diluted": (["DilutedEarningsPerShareBeforeExtraordinaryItems"], FLOW, PER_SHARE),
    "paid_up_capital": (["PaidUpValueOfEquityShareCapital"], FLOW, INR),
    "gross_npa": (["GrossNonPerformingAssets"], STOCK, INR),
    "net_npa": (["NonPerformingAssets"], STOCK, INR),
    "gross_npa_pct": (["PercentageOfGrossNpa"], STOCK, PURE),
    "net_npa_pct": (["PercentageOfNpa"], STOCK, PURE),
    "cet1_ratio": (["CET1Ratio"], STOCK, PURE),
    "total_assets": (["Assets"], STOCK, INR),
    "capital": (["Capital"], STOCK, INR),
    "reserves": (["ReservesAndSurplus"], STOCK, INR),
    "deposits": (["Deposits"], STOCK, INR),
    "borrowings": (["Borrowings"], STOCK, INR),
    "advances": (["Advances"], STOCK, INR),
    "investments": (["Investments"], STOCK, INR),
    "cfo": (["CashFlowsFromUsedInOperatingActivities"], CASHFLOW, INR),
    "cfi": (["CashFlowsFromUsedInInvestingActivities"], CASHFLOW, INR),
    "cff": (["CashFlowsFromUsedInFinancingActivities"], CASHFLOW, INR),
}
MAPPINGS = {"general": GENERAL, "banking": BANKING}
MAPPING_VERSION = "results-v1"
META = ("DateOfStartOfReportingPeriod", "DateOfEndOfReportingPeriod", "DateOfStartOfFinancialYear",
        "DateOfEndOfFinancialYear", "NatureOfReportStandaloneConsolidated",
        "WhetherResultsAreAuditedOrUnaudited", "ISIN", "Symbol", "ScripCode")


@dataclass(frozen=True)
class Fact:
    value: Decimal
    element: str
    context_kind: str      # period | ytd | instant_end
    start: date | None
    end: date              # instant for stock facts
    decimals: str | None
    unit: str


@dataclass
class ParsedResults:
    taxonomy: str | None = None
    template: str | None = None
    isin: str | None = None
    symbol: str | None = None
    basis: str | None = None           # standalone | consolidated
    audited: bool | None = None
    period_start: date | None = None
    period_end: date | None = None
    fy_start: date | None = None
    period_type: str | None = None     # Q | H | 9M | FY | other
    facts: dict[str, Fact] = field(default_factory=dict)
    context_counts: dict[str, int] = field(default_factory=dict)
    problems: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    mapping_version: str = MAPPING_VERSION

    @property
    def accepted(self) -> bool:
        return not self.problems

    def reject(self, code: str, detail: str = "") -> "ParsedResults":
        self.problems.append((code, detail))
        return self


def template_from_name(name: str | None) -> str | None:
    """NSE's file names carry the template: BANKING_..., NBFC_INDAS_..., etc."""
    upper = (name or "").rsplit("/", 1)[-1].upper()
    if "BANKING" in upper:
        return "banking"
    if re.search(r"(^|_)(INDAS|NONINDAS|NBFC)", upper):
        return "general"
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _date(text: str | None) -> date | None:
    try:
        return date.fromisoformat((text or "").strip()[:10])
    except ValueError:
        return None


def period_type(start: date, end: date) -> str:
    days = (end - start).days + 1
    if 88 <= days <= 93:
        return "Q"
    if 180 <= days <= 185:
        return "H"
    if 271 <= days <= 276:
        return "9M"
    if 364 <= days <= 367:
        return "FY"
    return "other"


def parse_results(body: bytes, *, name_hint: str | None = None) -> ParsedResults:
    out = ParsedResults()
    if len(body) > MAX_BYTES:
        return out.reject("too_large", f"{len(body)} bytes")
    if re.search(rb"<!DOCTYPE|<!ENTITY", body, re.I):
        return out.reject("unsafe_xml", "DOCTYPE/ENTITY present")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        return out.reject("malformed_xml", str(exc)[:120])
    if _local(root.tag) != "xbrl":
        return out.reject("not_xbrl", f"root element {_local(root.tag)!r}")

    prefixes = set(re.findall(rb'xmlns:([A-Za-z0-9_-]+)=', body[:20000]))
    out.taxonomy = next((p.decode() for p in (b"in-capmkt", b"in-bse-fin") if p in prefixes), None)

    contexts, units, facts = {}, {}, []
    for el in root:
        name = _local(el.tag)
        if name == "context":
            dims = any(_local(x.tag) in ("explicitMember", "typedMember") for x in el.iter())
            start = end = instant = None
            for x in el.iter():
                n = _local(x.tag)
                if n == "startDate":
                    start = _date(x.text)
                elif n == "endDate":
                    end = _date(x.text)
                elif n == "instant":
                    instant = _date(x.text)
            contexts[el.get("id")] = (dims, start, end, instant)
        elif name == "unit":
            measures = [(_local(m.tag), (m.text or "").split(":")[-1].strip()) for m in el.iter()
                        if _local(m.tag) == "measure"]
            vals = [v for _, v in measures]
            units[el.get("id")] = (INR if vals == ["INR"] else PER_SHARE if vals == ["INR", "shares"]
                                   else PURE if vals == ["pure"] else "/".join(vals))
        elif el.get("contextRef"):
            nil = any(k.endswith("}nil") and v == "true" for k, v in el.attrib.items())
            if not nil:
                facts.append((name, el.get("contextRef"), (el.text or "").strip(),
                              el.get("unitRef"), el.get("decimals")))

    # Filing metadata, from non-dimensional contexts only; conflicting values reject.
    meta: dict[str, set] = {k: set() for k in META}
    for name, ctx, text, _, _ in facts:
        if name in meta and ctx in contexts and not contexts[ctx][0] and text:
            meta[name].add(text)
    for k, vals in meta.items():
        if len(vals) > 1:
            out.reject("conflicting_meta", f"{k}: {sorted(vals)[:3]}")
    one = {k: next(iter(v)) if v else None for k, v in meta.items()}
    out.period_start = _date(one["DateOfStartOfReportingPeriod"])
    out.period_end = _date(one["DateOfEndOfReportingPeriod"])
    out.fy_start = _date(one["DateOfStartOfFinancialYear"])
    out.isin = (one["ISIN"] or "").strip()[:12] or None
    out.symbol = (one["Symbol"] or "").strip() or None
    nature = (one["NatureOfReportStandaloneConsolidated"] or "").lower()
    out.basis = "consolidated" if "consolidated" in nature else "standalone" if "standalone" in nature else None
    audited = (one["WhetherResultsAreAuditedOrUnaudited"] or "").lower()
    out.audited = None if not audited else not audited.startswith("un")

    if not out.period_start or not out.period_end or out.period_end <= out.period_start:
        return out.reject("missing_period", f"{one['DateOfStartOfReportingPeriod']}..{one['DateOfEndOfReportingPeriod']}")
    if out.basis is None:
        out.reject("missing_basis", repr(one["NatureOfReportStandaloneConsolidated"]))
    out.period_type = period_type(out.period_start, out.period_end)

    names = {f[0] for f in facts}
    out.template = template_from_name(name_hint) or ("banking" if "InterestEarned" in names else "general")

    def kind_of(ctx_id):
        dims, start, end, instant = contexts.get(ctx_id, (True, None, None, None))
        if dims:
            return None
        if instant is not None:
            return "instant_end" if instant == out.period_end else None
        if start == out.period_start and end == out.period_end:
            return "period"
        if out.fy_start and start == out.fy_start and end == out.period_end:
            return "ytd"
        return None

    for ctx_id in contexts:
        k = kind_of(ctx_id) or ("dimensional" if contexts[ctx_id][0] else "other")
        out.context_counts[k] = out.context_counts.get(k, 0) + 1

    allowed = {FLOW: ("period",), STOCK: ("instant_end",), CASHFLOW: ("ytd", "period")}
    by_element: dict[str, dict[str, set]] = {}
    for name, ctx, text, unit_ref, dec in facts:
        k = kind_of(ctx)
        if k:
            by_element.setdefault(name, {}).setdefault(k, set()).add((text, unit_ref, dec))

    for canonical, (candidates, kind, unit) in MAPPINGS[out.template].items():
        for element in candidates:
            found = by_element.get(element, {})
            ctx_kind = next((k for k in allowed[kind] if k in found), None)
            if ctx_kind is None:
                continue
            values = found[ctx_kind]
            if len({v[0] for v in values}) > 1:
                out.reject("conflicting_facts", f"{element} has {len(values)} values in {ctx_kind}")
                break
            text, unit_ref, dec = next(iter(values))
            got_unit = units.get(unit_ref)
            if got_unit != unit:
                out.reject("unit_mismatch", f"{element}: expected {unit}, got {got_unit or unit_ref}")
                break
            try:
                value = Decimal(text)
            except InvalidOperation:
                out.reject("non_numeric_value", f"{element}: {text[:20]!r}")
                break
            dims, start, end, instant = contexts[next(c for n, c, t, *_ in facts
                                                      if n == element and kind_of(c) == ctx_kind)]
            out.facts[canonical] = Fact(value, element, ctx_kind, start, instant or end, dec, unit)
            break

    _validate(out)
    return out


def _tolerance(*values: Decimal) -> Decimal:
    """Rounding slack: two lakh (filings round to the lakh), or 0.1%."""
    return max(Decimal(200000), max(abs(v) for v in values) * Decimal("0.001"))


def _validate(out: ParsedResults) -> None:
    f = {k: v.value for k, v in out.facts.items()}
    if "pat" not in f and "pbt" not in f:
        out.reject("no_core_facts", "neither profit before tax nor profit after tax found")
    if f.get("revenue") is not None and f["revenue"] < 0:
        out.reject("negative_revenue", str(f["revenue"]))
    if f.get("total_assets") is not None and f["total_assets"] <= 0:
        out.reject("non_positive_assets", str(f["total_assets"]))
    if all(k in f for k in ("total_income", "revenue", "other_income")):
        if abs(f["total_income"] - f["revenue"] - f["other_income"]) > _tolerance(f["total_income"]):
            out.warnings.append(("income_identity", "total income != revenue + other income"))
    if all(k in f for k in ("pbt", "tax", "pat")) and "pat_continuing" not in f:
        if abs(f["pbt"] - f["tax"] - f["pat"]) > _tolerance(f["pbt"], f["pat"]):
            out.warnings.append(("profit_identity", "PAT != PBT - tax"))
    if "pat" in f and "eps_basic" in f and f["pat"] != 0 and f["eps_basic"] != 0:
        if (f["pat"] > 0) != (f["eps_basic"] > 0):
            out.warnings.append(("eps_sign", "EPS and PAT have opposite signs"))
