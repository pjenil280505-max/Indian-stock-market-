"""Phase 2.0 source probe: offline checks of its pure helpers and its safety.

The probe itself only runs on a GitHub runner (NSE blocks other egress); these
tests pin that it never touches the database, never prints record contents
beyond timestamps, and refuses hostile XML before parsing it.
"""
import importlib.util
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "probe_phase2_sources.py"
WORKFLOW = ROOT / ".github" / "workflows" / "phase2-source-probe.yml"


def load():
    spec = importlib.util.spec_from_file_location("probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nse_date_format():
    assert load().nse_date(date(2026, 10, 1)) == "01-10-2026"


def test_records_of_accepts_both_shapes_and_nothing_else():
    p = load()
    assert p.records_of([{"a": 1}, "x"]) == [{"a": 1}]
    assert p.records_of({"data": [{"a": 1}]}) == [{"a": 1}]
    assert p.records_of({"msg": "no data"}) == [] and p.records_of("html") == []


def test_field_summary_shows_timestamps_but_never_other_values():
    p = load()
    recs = [{"symbol": "AAA", "desc": "SECRET HEADLINE", "an_dt": "01-Oct-2026 18:34:12", "seq_id": 7},
            {"symbol": "BBB", "desc": "", "an_dt": "01-Oct-2026 19:00:00"}]
    s = p.field_summary(recs)
    assert s["fields"]["desc"] == "str 1/2" and s["fields"]["seq_id"] == "int 1/2"
    assert s["timestamp_examples"] == {"an_dt": "01-Oct-2026 18:34:12"}
    assert "SECRET HEADLINE" not in repr(s) and "AAA" not in repr(s)


def test_xml_links_finds_only_xml_urls():
    p = load()
    recs = [{"xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/A.xml", "pdf": "https://x/y.pdf"}]
    assert p.xml_links(recs) == ["https://nsearchives.nseindia.com/corporate/xbrl/A.xml"]


def test_inspect_xbrl_refuses_entity_expansion_and_junk():
    p = load()
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    assert "DOCTYPE" in p.inspect_xbrl(bomb)["rejected"]
    assert "not well-formed" in p.inspect_xbrl(b"<html><body>Access Denied")["rejected"]
    assert "larger" in p.inspect_xbrl(b"x" * (p.MAX_XBRL_BYTES + 1))["rejected"]


def test_inspect_xbrl_reports_structure():
    p = load()
    doc = (b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in-bse-fin="urn:x">'
           b'<xbrli:context id="c1"/><in-bse-fin:ISIN contextRef="c1">INE002A01018</in-bse-fin:ISIN>'
           b'<in-bse-fin:ProfitBeforeTax contextRef="c1" unitRef="INR" decimals="-5">100</in-bse-fin:ProfitBeforeTax>'
           b'</xbrli:xbrl>')
    r = p.inspect_xbrl(doc)
    assert r["root"] == "xbrl" and r["contexts"] == 1 and r["isin"] == "INE002A01018"
    assert r["units"] == {"INR": 1} and r["decimals"] == {"-5": 1}
    assert "ProfitBeforeTax" in r["targets_present"] and "in-bse-fin" in r["namespace_prefixes"]


def test_probe_never_touches_the_database_or_secrets():
    src = SCRIPT.read_text()
    for forbidden in ("psycopg", "DATABASE_URL", "connect(", "UPSTOX", "Authorization", "Cookie"):
        assert forbidden not in src, forbidden


def test_probe_workflow_is_branch_only_secretless_and_read_only():
    text = WORKFLOW.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "secrets." not in code
    assert "branches: [claude/indian-stock-research-phase-0-o6zage]" in code
    assert "schedule:" not in code and "workflow_dispatch:" not in code
    assert "permissions:\n  contents: read\n" in text
