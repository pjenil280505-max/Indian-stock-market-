"""Issuer identity (Phase 2.1).

An ISIN names a security, not a company. Indian equity ISINs look like

    IN E 002A 01 01 8
    |  |  |    |  |  check digit
    |  |  |    |  serial  - changes when the security is re-issued (e.g. a split)
    |  |  |    security type (01 = equity shares)
    |  |  issuer code - stays with the company
    |  issuer type: E = company; 9 = the same company's partly-paid/DVR shares
    country

so a company's financial history must hang off the issuer code, not off
symbol_id. Mutual funds/ETFs (INF...) and other issuer types are not
companies and are left unmapped, with a finding, rather than guessed.

Ticker aliases are recorded from the first observation onwards. Earlier
ticker history is not available in this database and is not invented.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

ISIN_SHAPE = re.compile(r"^[A-Z]{2}[0-9A-Z]{9}[0-9]$")
COMPANY_TYPES = {"E": "E", "9": "E"}  # 9-series are the same company's other equity


def isin_check_digit(first_eleven: str) -> int:
    """ISO 6166 check digit: letters -> 10..35, then Luhn over the digits."""
    digits = "".join(str(int(ch, 36)) for ch in first_eleven)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def isin_problem(isin: str | None) -> str | None:
    """Why an ISIN is unusable, or None if it is a valid Indian ISIN."""
    if not isin:
        return "missing"
    if not ISIN_SHAPE.match(isin):
        return "malformed"
    if not isin.startswith("IN"):
        return "not Indian"
    if isin_check_digit(isin[:11]) != int(isin[11]):
        return "bad check digit"
    return None


def issuer_code(isin: str | None) -> str | None:
    """Company key from an Indian equity ISIN, or None if not a company ISIN."""
    if isin_problem(isin):
        return None
    kind = COMPANY_TYPES.get(isin[2])
    return None if kind is None else kind + isin[3:7]


# ---- database sync -------------------------------------------------------------

@dataclass
class IdentitySummary:
    symbols_seen: int = 0
    issuers_upserted: int = 0
    links_created: int = 0
    aliases_recorded: int = 0
    findings: list = field(default_factory=list)  # integrity.Finding

    def as_text(self) -> str:
        return (f"symbols_seen={self.symbols_seen} issuers_upserted={self.issuers_upserted}"
                f" links_created={self.links_created} aliases_recorded={self.aliases_recorded}"
                f" findings={len(self.findings)}")


def sync_identity(conn, today: date) -> IdentitySummary:
    """Bring issuers, symbol_issuer and symbol_aliases up to date from symbols.

    Idempotent: re-running on the same day changes nothing. Reads Phase 1's
    symbols table and writes only the Phase 2 identity tables. Commits once.
    """
    from .integrity import Finding

    out = IdentitySummary()
    with conn.cursor() as cur:
        cur.execute("SELECT symbol_id, isin, symbol, name FROM symbols ORDER BY symbol_id")
        rows = cur.fetchall()
        out.symbols_seen = len(rows)

        mappable = []
        for symbol_id, isin, symbol, name in rows:
            problem = isin_problem(isin)
            code = None if problem else issuer_code(isin)
            if problem:
                out.findings.append(Finding("invalid_isin", "warning",
                                            f"{symbol} {isin!r}: {problem}", symbol_id, today))
            elif code is None:
                out.findings.append(Finding("non_company_isin", "info",
                                            f"{symbol} {isin}: issuer type {isin[2]!r} is not a company",
                                            symbol_id, today))
            else:
                mappable.append((symbol_id, code, symbol, name))

        # Issuers: first-seen name kept; last_seen advances.
        cur.executemany(
            """
            INSERT INTO issuers (issuer_code, name, first_seen, last_seen)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (issuer_code) DO UPDATE SET
                name      = COALESCE(issuers.name, EXCLUDED.name),
                last_seen = GREATEST(issuers.last_seen, EXCLUDED.last_seen)
            """,
            [(code, name, today, today) for _, code, _, name in mappable],
        )
        out.issuers_upserted = len({code for _, code, _, _ in mappable})

        # Links: set once, never re-pointed silently.
        cur.execute("SELECT symbol_id, issuer_id FROM symbol_issuer")
        linked = dict(cur.fetchall())
        cur.execute("SELECT issuer_code, issuer_id FROM issuers")
        ids = dict(cur.fetchall())
        new_links = []
        for symbol_id, code, symbol, _ in mappable:
            want = ids[code]
            have = linked.get(symbol_id)
            if have is None:
                new_links.append((symbol_id, want))
            elif have != want:
                out.findings.append(Finding("issuer_link_conflict", "error",
                                            f"{symbol}: linked to issuer {have}, ISIN says {want}",
                                            symbol_id, today))
        cur.executemany(
            "INSERT INTO symbol_issuer (symbol_id, issuer_id, method) VALUES (%s, %s, 'isin_issuer_code')"
            " ON CONFLICT (symbol_id) DO NOTHING",
            new_links,
        )
        out.links_created = len(new_links)

        # Several securities per issuer is legitimate (partly-paid, DVR, a
        # re-issued ISIN after a split) but worth a human look once.
        cur.execute(
            "SELECT i.issuer_code, array_agg(s.symbol ORDER BY s.symbol) FROM symbol_issuer l"
            " JOIN issuers i USING (issuer_id) JOIN symbols s USING (symbol_id)"
            " WHERE l.symbol_id = ANY(%s)"
            " GROUP BY i.issuer_code HAVING count(*) > 1",
            ([sid for sid, _ in new_links],),
        )
        for code, symbols in cur.fetchall():
            out.findings.append(Finding("issuer_multi_security", "info",
                                        f"issuer {code}: {', '.join(symbols)}", None, today))

        # Ticker aliases as observation intervals (same rule as universe_membership).
        cur.execute("SELECT max(valid_to) FROM symbol_aliases WHERE valid_to < %s", (today,))
        previous = cur.fetchone()[0] or today
        cur.executemany(
            """
            WITH extended AS (
                UPDATE symbol_aliases SET valid_to = %s
                 WHERE symbol_id = %s AND ticker = %s AND valid_to >= %s AND valid_from <= %s
                RETURNING 1
            )
            INSERT INTO symbol_aliases (symbol_id, ticker, valid_from, valid_to)
            SELECT %s, %s, %s, %s WHERE NOT EXISTS (SELECT 1 FROM extended)
            """,
            [(today, sid, sym, previous, today, sid, sym, today, today) for sid, isin, sym, _ in rows if sym],
        )
        out.aliases_recorded = sum(1 for _, _, sym, _ in rows if sym)
    conn.commit()
    return out
