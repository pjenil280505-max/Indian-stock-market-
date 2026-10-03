"""Financial-results XBRL parser: offline, against synthetic instances built
to the structure measured from real NSE filings in step 2.0 (element names,
contexts, units, decimals). Real filings are exercised separately on a
GitHub runner; NSE files are not committed to this public repository."""
from datetime import date
from decimal import Decimal

import pytest

from src.fundamentals.results_xbrl import parse_results, period_type, template_from_name

NS = ('xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:iso4217="http://www.xbrl.org/2003/iso4217"'
      ' xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:in-capmkt="urn:in-capmkt"'
      ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')
UNITS = ('<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>'
         '<xbrli:unit id="INRPerShare"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:INR'
         '</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares'
         '</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>'
         '<xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>')


def ctx(cid, start=None, end=None, instant=None, dim=False):
    period = (f"<xbrli:instant>{instant}</xbrli:instant>" if instant else
              f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>")
    seg = ('<xbrli:segment><xbrldi:explicitMember dimension="in-capmkt:X">in-capmkt:Y</xbrldi:explicitMember>'
           '</xbrli:segment>') if dim else ""
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="s">C</xbrli:identifier>{seg}'
            f'</xbrli:entity><xbrli:period>{period}</xbrli:period></xbrli:context>')


def fact(name, cid, value, unit="INR", decimals="-5"):
    attrs = f' unitRef="{unit}" decimals="{decimals}"' if unit else ""
    return f'<in-capmkt:{name} contextRef="{cid}"{attrs}>{value}</in-capmkt:{name}>'


def meta(cid="Q", start="2026-04-01", end="2026-06-30", nature="Standalone", fy="2026-04-01"):
    return "".join(fact(n, cid, v, unit=None) for n, v in (
        ("DateOfStartOfReportingPeriod", start), ("DateOfEndOfReportingPeriod", end),
        ("DateOfStartOfFinancialYear", fy), ("NatureOfReportStandaloneConsolidated", nature),
        ("WhetherResultsAreAuditedOrUnaudited", "Unaudited"), ("ISIN", "INE002A01018"), ("Symbol", "RELIANCE")))


def doc(*parts, contexts=None):
    contexts = contexts if contexts is not None else [
        ctx("Q", "2026-04-01", "2026-06-30"), ctx("PY", "2025-04-01", "2025-06-30"),
        ctx("I", instant="2026-06-30"), ctx("SEG", "2026-04-01", "2026-06-30", dim=True)]
    return f'<xbrli:xbrl {NS}>{"".join(contexts)}{UNITS}{meta()}{"".join(parts)}</xbrli:xbrl>'.encode()


PNL = [fact("RevenueFromOperations", "Q", "1000000000"), fact("OtherIncome", "Q", "50000000"),
       fact("Income", "Q", "1050000000"), fact("ProfitBeforeTax", "Q", "200000000"),
       fact("TaxExpense", "Q", "50000000"), fact("ProfitLossForPeriod", "Q", "150000000"),
       fact("BasicEarningsLossPerShareFromContinuingOperations", "Q", "2.5", unit="INRPerShare", decimals="2")]


def test_reads_current_period_only_and_ignores_comparatives_and_segments():
    r = parse_results(doc(*PNL, fact("RevenueFromOperations", "PY", "900000000"),
                          fact("RevenueFromOperations", "SEG", "1"), fact("Assets", "I", "5000000000")))
    assert r.accepted, r.problems
    assert r.facts["revenue"].value == Decimal("1000000000")
    assert r.facts["revenue"].context_kind == "period" and r.facts["revenue"].start == date(2026, 4, 1)
    assert r.facts["total_assets"].context_kind == "instant_end"
    assert r.facts["eps_basic"].unit == "INRPerShare" and r.facts["eps_basic"].value == Decimal("2.5")
    assert (r.basis, r.audited, r.period_type, r.isin, r.taxonomy) == (
        "standalone", False, "Q", "INE002A01018", "in-capmkt")
    assert r.context_counts == {"period": 1, "other": 1, "instant_end": 1, "dimensional": 1}
    assert r.warnings == []


def test_missing_fact_stays_missing_never_zero():
    r = parse_results(doc(*PNL))
    assert "total_assets" not in r.facts and "cfo" not in r.facts


def test_cash_flow_comes_from_the_year_to_date_context():
    contexts = [ctx("Q", "2026-07-01", "2026-09-30"), ctx("YTD", "2026-04-01", "2026-09-30")]
    body = doc(fact("ProfitBeforeTax", "Q", "1"), fact("ProfitLossForPeriod", "Q", "1"),
               fact("CashFlowsFromUsedInOperatingActivities", "YTD", "777"), contexts=contexts)
    body = body.replace(b"2026-04-01</in-capmkt:DateOfStartOfReportingPeriod",
                        b"2026-07-01</in-capmkt:DateOfStartOfReportingPeriod").replace(
                        b"2026-06-30</in-capmkt:DateOfEndOfReportingPeriod",
                        b"2026-09-30</in-capmkt:DateOfEndOfReportingPeriod")
    r = parse_results(body)
    assert r.accepted, r.problems
    assert r.facts["cfo"].value == 777 and r.facts["cfo"].context_kind == "ytd"


def test_banking_template_uses_bank_element_names():
    bank = [fact("InterestEarned", "Q", "900"), fact("ProfitLossFromOrdinaryActivitiesBeforeTax", "Q", "300"),
            fact("TaxExpense", "Q", "100"), fact("ProfitLossForThePeriod", "Q", "200"),
            fact("GrossNonPerformingAssets", "I", "50"), fact("PercentageOfGrossNpa", "I", "1.5", unit="pure")]
    r = parse_results(doc(*bank), name_hint="https://x/BANKING_123_456_789.xml")
    assert r.template == "banking" and r.accepted, r.problems
    assert r.facts["pat"].element == "ProfitLossForThePeriod"
    assert r.facts["gross_npa_pct"].unit == "pure" and "revenue" not in r.facts


def test_eps_prefers_total_over_continuing_operations():
    r = parse_results(doc(*PNL, fact("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                                     "Q", "2.4", unit="INRPerShare", decimals="2")))
    assert r.facts["eps_basic"].value == Decimal("2.4")


@pytest.mark.parametrize("extra,code", [
    ([fact("ProfitBeforeTax", "Q", "999")], "conflicting_facts"),
    ([fact("Assets", "I", "5", unit="pure")], "unit_mismatch"),
    ([fact("Assets", "I", "lots")], "non_numeric_value"),
])
def test_bad_facts_are_rejected_with_a_reason(extra, code):
    r = parse_results(doc(*PNL, *extra))
    assert not r.accepted and r.problems[0][0] == code


def test_negative_revenue_is_rejected():
    pnl = [p.replace(">1000000000<", ">-1000000000<") for p in PNL]
    r = parse_results(doc(*pnl))
    assert ("negative_revenue", "-1000000000") in r.problems


@pytest.mark.parametrize("body,code", [
    (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "a">]><x>&a;</x>', "unsafe_xml"),
    (b"<html><body>Access Denied</body></html>", "not_xbrl"),
    (b"<xbrl", "malformed_xml"),
    (b"x" * (20 * 1024 * 1024 + 1), "too_large"),
])
def test_unsafe_or_junk_documents_are_refused_before_use(body, code):
    assert parse_results(body).problems[0][0] == code


def test_missing_period_basis_and_core_facts():
    no_basis = doc(*PNL).replace(b">Standalone<", b"><")
    assert [c for c, _ in parse_results(no_basis).problems] == ["missing_basis"]
    assert parse_results(doc(fact("RevenueFromOperations", "Q", "1"))).problems[0][0] == "no_core_facts"
    no_period = doc(*PNL).replace(b"2026-06-30</in-capmkt:DateOfEndOfReportingPeriod",
                                  b"</in-capmkt:DateOfEndOfReportingPeriod")
    assert parse_results(no_period).problems[0][0] == "missing_period"


def test_conflicting_metadata_is_rejected():
    body = doc(*PNL).replace(b"</xbrli:xbrl>", meta(nature="Consolidated").encode() + b"</xbrli:xbrl>")
    assert parse_results(body).problems[0][0] == "conflicting_meta"


def test_identity_mismatches_are_warnings_not_rejections():
    pnl = [p.replace(">1050000000<", ">2000000000<").replace(">150000000<", ">10000000<") for p in PNL]
    r = parse_results(doc(*pnl))
    assert r.accepted
    assert {w[0] for w in r.warnings} == {"income_identity", "profit_identity"}


def test_nil_facts_are_skipped():
    nil = '<in-capmkt:Assets contextRef="I" unitRef="INR" decimals="-5" xsi:nil="true"/>'
    r = parse_results(doc(*PNL, nil))
    assert r.accepted, r.problems
    assert "total_assets" not in r.facts


@pytest.mark.parametrize("start,end,kind", [
    ((2026, 4, 1), (2026, 6, 30), "Q"), ((2026, 4, 1), (2026, 9, 30), "H"),
    ((2026, 4, 1), (2026, 12, 31), "9M"), ((2026, 4, 1), (2027, 3, 31), "FY"), ((2026, 4, 1), (2026, 5, 31), "other"),
])
def test_period_type(start, end, kind):
    assert period_type(date(*start), date(*end)) == kind


@pytest.mark.parametrize("name,template", [
    ("https://x/INTEGRATED_FILING_INDAS_1_2_WEB.xml", "general"), ("NBFC_INDAS_1.xml", "general"),
    ("NONINDAS_1.xml", "general"), ("BANKING_1.xml", "banking"), ("whatever.xml", None),
])
def test_template_from_name(name, template):
    assert template_from_name(name) == template
