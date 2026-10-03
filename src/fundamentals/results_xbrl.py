"""Financial-results XBRL parser (Phase 2.3). Pure: bytes in, typed facts out.

Handles the NSE results XBRL families measured in step 2.0:

  taxonomy in-bse-fin   classic results endpoint, 2018 - Feb 2025
  taxonomy in-capmkt    integrated filings, quarter ending Mar 2025 onwards
  templates             general Ind AS (incl. NBFC and non-Ind-AS) and banking,
                        which uses different element names

Rules, all deliberate:

  * A filing carries one or more REPORTING COLUMNS, each declared by a
    context that states its own DateOfStart/EndOfReportingPeriod (a Q2
    filing: the quarter and the half-year to date; Q4: the quarter and the
    full year). Measured on real filings, run 37098560580. The primary
    column is the shortest one ending last - the quarter.
  * Only NON-dimensional contexts are read (dimensional ones are segments
    and breakdowns, not company totals). Profit and loss and cash flow come
    from each column's own period; balance sheet from the instant at the
    column's end. Comparative columns of earlier periods are ignored - the
    earlier filing is the point-in-time source for those.
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
FLOW, STOCK, CASHFLOW = "flow", "stock", "cashflow"  # cash flow is a flow of its column
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


@dataclass(frozen=True)
class Fact:
    value: Decimal
    element: str
    start: date | None     # None for balance-sheet (instant) facts
    end: date              # period end, or the balance-sheet instant
    decimals: str | None
    unit: str


@dataclass
class Column:
    """One reporting period inside a filing (a filing for Q2 carries the
    quarter AND the half-year to date; a Q4 filing the quarter AND the year)."""
    start: date
    end: date
    period_type: str
    audited: bool | None = None
    facts: dict[str, Fact] = field(default_factory=dict)


@dataclass
class ParsedResults:
    taxonomy: str | None = None
    template: str | None = None
    isin: str | None = None
    symbol: str | None = None
    basis: str | None = None           # standalone | consolidated
    columns: list[Column] = field(default_factory=list)   # primary (latest, shortest) first
    context_counts: dict[str, int] = field(default_factory=dict)
    problems: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    mapping_version: str = MAPPING_VERSION

    @property
    def accepted(self) -> bool:
        return not self.problems

    @property
    def primary(self) -> Column | None:
        return self.columns[0] if self.columns else None

    @property
    def facts(self) -> dict[str, Fact]:
        return self.primary.facts if self.primary else {}

    @property
    def period_start(self):
        return self.primary.start if self.primary else None

    @property
    def period_end(self):
        return self.primary.end if self.primary else None

    @property
    def period_type(self):
        return self.primary.period_type if self.primary else None

    @property
    def audited(self):
        return self.primary.audited if self.primary else None

    def reject(self, code: str, detail: str = "") -> "ParsedResults":
        self.problems.append((code, detail))
        return self

    def warn(self, code: str, detail: str = "") -> None:
        if (code, detail) not in self.warnings:
            self.warnings.append((code, detail))


def template_from_name(name: str | None) -> str | None:
    """NSE's file names carry the template: BANKING_..., NBFC_INDAS_..., etc."""
    upper = (name or "").rsplit("/", 1)[-1].upper()
    if "BANKING" in upper:
        return "banking"
    if re.search(r"(^|_)(INDAS|NONINDAS|NBFC)", upper):
        return "general"
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


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


def _audited(text: str | None) -> bool | None:
    t = (text or "").strip().lower()
    return None if not t else not t.startswith("un")


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

    # Contexts, units and facts may sit at any depth (older filings nest them).
    contexts, units, facts = {}, {}, []
    for el in root.iter():
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
            vals = [(m.text or "").split(":")[-1].strip() for m in el.iter() if _local(m.tag) == "measure"]
            units[el.get("id")] = (INR if vals == ["INR"] else PER_SHARE if vals == ["INR", "shares"]
                                   else PURE if vals == ["pure"] else "/".join(vals))
        elif el.get("contextRef"):
            nil = any(k.endswith("}nil") and v == "true" for k, v in el.attrib.items())
            if not nil:
                facts.append((name, el.get("contextRef"), (el.text or "").strip(),
                              el.get("unitRef"), el.get("decimals")))

    def plain(ctx_id):
        c = contexts.get(ctx_id)
        return c is not None and not c[0]

    # Filing-level metadata: one value across all plain contexts, or reject.
    single: dict[str, set] = {k: set() for k in ("NatureOfReportStandaloneConsolidated", "ISIN", "Symbol")}
    for name, ctx, text, _, _ in facts:
        if name in single and text and plain(ctx):
            single[name].add(text.strip())
    for k, vals in single.items():
        if len(vals) > 1:
            out.reject("conflicting_meta", f"{k}: {sorted(vals)[:3]}")
    one = {k: next(iter(v)) if v else None for k, v in single.items()}
    out.isin = (one["ISIN"] or "")[:12] or None
    out.symbol = one["Symbol"]
    nature = (one["NatureOfReportStandaloneConsolidated"] or "").lower()
    out.basis = "consolidated" if "consolidated" in nature else "standalone" if "standalone" in nature else None
    if out.basis is None:
        out.reject("missing_basis", repr(one["NatureOfReportStandaloneConsolidated"]))

    # Reporting columns: plain duration contexts that state their own period.
    stated: dict[str, dict[str, str]] = {}
    for name, ctx, text, _, _ in facts:
        if name in ("DateOfStartOfReportingPeriod", "DateOfEndOfReportingPeriod",
                    "WhetherResultsAreAuditedOrUnaudited") and plain(ctx):
            stated.setdefault(ctx, {})[name] = text
    columns: dict[tuple, Column] = {}
    column_of: dict[str, tuple] = {}
    for ctx, vals in stated.items():
        s, e = _date(vals.get("DateOfStartOfReportingPeriod")), _date(vals.get("DateOfEndOfReportingPeriod"))
        if not s or not e:
            continue
        _, cs, ce, _ = contexts[ctx]
        if (cs, ce) != (s, e) or e <= s:
            out.reject("period_mismatch", f"context {ctx} is {cs}..{ce} but states {s}..{e}")
            continue
        col = columns.setdefault((s, e), Column(s, e, period_type(s, e)))
        if col.audited is None:
            col.audited = _audited(vals.get("WhetherResultsAreAuditedOrUnaudited"))
        column_of[ctx] = (s, e)
    # Other plain contexts with exactly a column's dates belong to that column.
    for ctx, (dims, cs, ce, inst) in contexts.items():
        if not dims and inst is None and (cs, ce) in columns:
            column_of.setdefault(ctx, (cs, ce))
    if not columns:
        return out.reject("missing_period", "no context states its reporting period")
    out.columns = sorted(columns.values(), key=lambda c: (-c.end.toordinal(), (c.end - c.start).days))

    ends = {c.end for c in out.columns}
    for ctx_id, (dims, cs, ce, inst) in contexts.items():
        kind = ("dimensional" if dims else "column" if ctx_id in column_of
                else "instant" if inst in ends else "other")
        out.context_counts[kind] = out.context_counts.get(kind, 0) + 1

    names = {f[0] for f in facts}
    out.template = template_from_name(name_hint) or ("banking" if "InterestEarned" in names else "general")

    # Collect candidate values per (element, column-or-instant).
    found: dict[tuple, set] = {}
    for name, ctx, text, unit_ref, dec in facts:
        if not plain(ctx):
            continue
        dims, cs, ce, inst = contexts[ctx]
        where = ("i", inst) if inst is not None else ("d", column_of.get(ctx))
        if where[1] is None:
            continue
        found.setdefault((name, where), set()).add((text, unit_ref, dec))

    for col in out.columns:
        for canonical, (candidates, kind, unit) in MAPPINGS[out.template].items():
            where = ("i", col.end) if kind == STOCK else ("d", (col.start, col.end))
            for element in candidates:
                values = found.get((element, where))
                if not values:
                    continue
                numeric = {v for v in values if v[1]}
                if not numeric:
                    out.warn("unitless_fact", element)
                    continue
                if len({v[0] for v in numeric}) > 1:
                    out.reject("conflicting_facts", f"{element} has {len(numeric)} values for {where[1]}")
                    break
                text, unit_ref, dec = next(iter(numeric))
                got_unit = units.get(unit_ref)
                if got_unit != unit:
                    out.reject("unit_mismatch", f"{element}: expected {unit}, got {got_unit or unit_ref}")
                    break
                try:
                    value = Decimal(text)
                except InvalidOperation:
                    out.reject("non_numeric_value", f"{element}: {text[:20]!r}")
                    break
                col.facts[canonical] = Fact(value, element, None if kind == STOCK else col.start,
                                            col.end, dec, unit)
                break

    _validate(out)
    return out


def _tolerance(*values: Decimal) -> Decimal:
    """Rounding slack: two lakh (filings round to the lakh), or 0.1%."""
    return max(Decimal(200000), max(abs(v) for v in values) * Decimal("0.001"))


def _validate(out: ParsedResults) -> None:
    for i, col in enumerate(out.columns):
        _validate_column(out, col, primary=(i == 0))


def _validate_column(out: ParsedResults, col: Column, primary: bool) -> None:
    f = {k: v.value for k, v in col.facts.items()}
    tag = f"{col.start}..{col.end}"
    if "pat" not in f and "pbt" not in f:
        if primary:
            out.reject("no_core_facts", f"{tag}: neither profit before tax nor profit after tax found")
        else:
            out.warn("column_without_core", tag)
    if f.get("revenue") is not None and f["revenue"] < 0:
        out.reject("negative_revenue", str(f["revenue"]))
    if f.get("total_assets") is not None and f["total_assets"] <= 0:
        out.reject("non_positive_assets", str(f["total_assets"]))
    if all(k in f for k in ("total_income", "revenue", "other_income")):
        if abs(f["total_income"] - f["revenue"] - f["other_income"]) > _tolerance(f["total_income"]):
            out.warn("income_identity", f"{tag}: total income != revenue + other income")
    if all(k in f for k in ("pbt", "tax", "pat")) and "pat_continuing" not in f:
        if abs(f["pbt"] - f["tax"] - f["pat"]) > _tolerance(f["pbt"], f["pat"]):
            out.warn("profit_identity", f"{tag}: PAT != PBT - tax")
    if "pat" in f and "eps_basic" in f and f["pat"] != 0 and f["eps_basic"] != 0:
        if (f["pat"] > 0) != (f["eps_basic"] > 0):
            out.warn("eps_sign", f"{tag}: EPS and PAT have opposite signs")
