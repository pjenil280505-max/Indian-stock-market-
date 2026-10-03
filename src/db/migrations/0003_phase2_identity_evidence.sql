-- Migration 0003 - Phase 2.1: issuer identity and the raw-evidence register.
--
-- ADDITIVE ONLY. Creates four new tables and one trigger function; it does
-- not alter, read or write any Phase 1 table. Phase 1 data is untouched.
--
-- Why issuers: an ISIN identifies a SECURITY, and Indian ISINs change when a
-- company splits its shares (the serial digits move), so symbol_id cannot carry
-- a company's financial history across a split. The issuer code inside the
-- ISIN (characters 4-7) stays put; see src/identity.py.
--
-- Why source_documents: every raw payload fetched for Phase 2 is kept as
-- immutable evidence in object storage (Cloudflare R2), content-addressed by
-- SHA-256. This table is the register of what was fetched, from where, when,
-- and which bytes. A source that later changes a file produces a NEW row,
-- never an overwrite - which is what makes "what did we know at time T"
-- answerable.

CREATE TABLE IF NOT EXISTS issuers (
    issuer_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    issuer_code TEXT NOT NULL UNIQUE,
    name        TEXT,
    first_seen  DATE NOT NULL,
    last_seen   DATE NOT NULL,
    CONSTRAINT issuer_code_shape CHECK (issuer_code ~ '^E[0-9A-Z]{4}$'),
    CONSTRAINT issuer_seen_order CHECK (last_seen >= first_seen)
);

-- Which company each security belongs to. One row per security, set once.
CREATE TABLE IF NOT EXISTS symbol_issuer (
    symbol_id BIGINT PRIMARY KEY REFERENCES symbols (symbol_id),
    issuer_id BIGINT NOT NULL REFERENCES issuers (issuer_id),
    method    TEXT   NOT NULL,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT symbol_issuer_method CHECK (method IN ('isin_issuer_code', 'manual'))
);
CREATE INDEX IF NOT EXISTS symbol_issuer_issuer_idx ON symbol_issuer (issuer_id);

-- Ticker history, observed from now on (symbols.symbol only holds the
-- current ticker). Intervals like universe_membership: valid_to is the last
-- date the ticker was observed. History before the first observation is NOT
-- reconstructed - it is not available, so it is not invented.
CREATE TABLE IF NOT EXISTS symbol_aliases (
    symbol_id  BIGINT NOT NULL REFERENCES symbols (symbol_id),
    ticker     TEXT   NOT NULL,
    valid_from DATE   NOT NULL,
    valid_to   DATE   NOT NULL,
    PRIMARY KEY (symbol_id, ticker, valid_from),
    CONSTRAINT symbol_alias_range CHECK (valid_to >= valid_from)
);
CREATE INDEX IF NOT EXISTS symbol_aliases_ticker_idx ON symbol_aliases (ticker, valid_from);

-- Register of raw evidence. The bytes live in object storage under
-- storage_key; this row records provenance. Evidence columns are immutable
-- (trigger below: UPDATE of evidence columns and DELETE are refused); only the
-- processing status may change. Wholesale table operations are kept out of
-- runtime code by tests/test_phase2_identity_evidence.py, and out of
-- migrations by tests/test_phase1a_guards.py.
CREATE TABLE IF NOT EXISTS source_documents (
    doc_id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source               TEXT    NOT NULL,
    source_record_id     TEXT    NOT NULL,
    url                  TEXT,
    sha256               TEXT    NOT NULL,
    bytes                INTEGER NOT NULL,
    content_type         TEXT,
    source_last_modified TEXT,
    storage_key          TEXT    NOT NULL,
    fetched_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id               BIGINT REFERENCES ingestion_runs (run_id),
    parse_status         TEXT    NOT NULL DEFAULT 'pending',
    reject_reason        TEXT,
    CONSTRAINT source_documents_version UNIQUE (source, source_record_id, sha256),
    CONSTRAINT source_documents_sha CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT source_documents_bytes CHECK (bytes >= 0),
    CONSTRAINT source_documents_source CHECK (source ~ '^[a-z0-9_]+$'),
    CONSTRAINT source_documents_status CHECK (parse_status IN ('pending', 'parsed', 'rejected')),
    CONSTRAINT source_documents_reason CHECK ((parse_status = 'rejected') = (reject_reason IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS source_documents_sha_idx ON source_documents (sha256);
CREATE INDEX IF NOT EXISTS source_documents_status_idx ON source_documents (parse_status)
    WHERE parse_status = 'pending';

CREATE OR REPLACE FUNCTION source_documents_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'source_documents is immutable evidence: delete refused (doc_id %)', OLD.doc_id;
    END IF;
    IF NEW.source               IS DISTINCT FROM OLD.source
       OR NEW.source_record_id  IS DISTINCT FROM OLD.source_record_id
       OR NEW.url               IS DISTINCT FROM OLD.url
       OR NEW.sha256            IS DISTINCT FROM OLD.sha256
       OR NEW.bytes             IS DISTINCT FROM OLD.bytes
       OR NEW.content_type      IS DISTINCT FROM OLD.content_type
       OR NEW.source_last_modified IS DISTINCT FROM OLD.source_last_modified
       OR NEW.storage_key       IS DISTINCT FROM OLD.storage_key
       OR NEW.fetched_at        IS DISTINCT FROM OLD.fetched_at
       OR NEW.run_id            IS DISTINCT FROM OLD.run_id THEN
        RAISE EXCEPTION 'source_documents evidence columns are immutable (doc_id %)', OLD.doc_id;
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE TRIGGER source_documents_immutable_rows
    BEFORE UPDATE OR DELETE ON source_documents
    FOR EACH ROW EXECUTE FUNCTION source_documents_immutable();
