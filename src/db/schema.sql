-- Phase 1 schema. Data foundation only: no strategy, signal or backtest tables.
--
-- Design rules carried from docs/PHASE_0_REPORT.md:
--   section 2.5  identity is ISIN, never the ticker (tickers get renamed)
--   section 2.4  raw and adjusted series are stored separately, never merged
--   section 6.2  point-in-time universe membership, for survivorship control

CREATE TABLE IF NOT EXISTS symbols (
    symbol_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    isin        TEXT NOT NULL UNIQUE,
    symbol      TEXT NOT NULL,
    name        TEXT,
    face_value  NUMERIC(18, 4),
    first_seen  DATE NOT NULL,
    last_seen   DATE NOT NULL
);
CREATE INDEX IF NOT EXISTS symbols_symbol_idx ON symbols (symbol);

-- Point-in-time universe membership.
-- One row per (snapshot_date, symbol). Reconstructing "what was listed on
-- date D" is then a single query, which is what makes survivorship-bias
-- control possible. This data CANNOT be backfilled later, which is why
-- archiving starts on day one of Phase 1.
CREATE TABLE IF NOT EXISTS universe_snapshots (
    snapshot_date DATE   NOT NULL,
    symbol_id     BIGINT NOT NULL REFERENCES symbols (symbol_id),
    series        TEXT   NOT NULL,
    listing_date  DATE,
    PRIMARY KEY (snapshot_date, symbol_id)
);
CREATE INDEX IF NOT EXISTS universe_snapshots_symbol_idx
    ON universe_snapshots (symbol_id, snapshot_date);

-- Raw, as-traded bars from NSE. NOT adjusted for corporate actions.
-- Use these for liquidity, tradability and position sizing.
CREATE TABLE IF NOT EXISTS daily_bars_raw (
    symbol_id   BIGINT NOT NULL REFERENCES symbols (symbol_id),
    trade_date  DATE   NOT NULL,
    series      TEXT   NOT NULL,
    open        NUMERIC(18, 4) NOT NULL,
    high        NUMERIC(18, 4) NOT NULL,
    low         NUMERIC(18, 4) NOT NULL,
    close       NUMERIC(18, 4) NOT NULL,
    prev_close  NUMERIC(18, 4),
    last_price  NUMERIC(18, 4),
    volume      BIGINT NOT NULL,
    turnover    NUMERIC(24, 4),
    trades      BIGINT,
    deliv_qty   BIGINT,
    deliv_pct   NUMERIC(9, 4),
    source      TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol_id, trade_date, series),
    CONSTRAINT daily_bars_raw_ohlc_sane CHECK (
        high >= low AND high >= open AND high >= close
        AND low <= open AND low <= close
        AND open > 0 AND close > 0 AND volume >= 0
    )
);
CREATE INDEX IF NOT EXISTS daily_bars_raw_date_idx ON daily_bars_raw (trade_date);

-- Corporate-action back-adjusted bars from Upstox.
-- Use these for indicators and signals. NEVER use adjusted volume for
-- liquidity filtering: it is inflated by past corporate actions
-- (report section 2.4 - Reliance volume doubled by the 1:1 bonus).
CREATE TABLE IF NOT EXISTS daily_bars_adjusted (
    symbol_id   BIGINT NOT NULL REFERENCES symbols (symbol_id),
    trade_date  DATE   NOT NULL,
    open        NUMERIC(18, 4) NOT NULL,
    high        NUMERIC(18, 4) NOT NULL,
    low         NUMERIC(18, 4) NOT NULL,
    close       NUMERIC(18, 4) NOT NULL,
    volume      BIGINT NOT NULL,
    source      TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol_id, trade_date),
    CONSTRAINT daily_bars_adjusted_ohlc_sane CHECK (
        high >= low AND high >= open AND high >= close
        AND low <= open AND low <= close
        AND open > 0 AND close > 0 AND volume >= 0
    )
);
CREATE INDEX IF NOT EXISTS daily_bars_adjusted_date_idx ON daily_bars_adjusted (trade_date);

CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol_id   BIGINT NOT NULL REFERENCES symbols (symbol_id),
    ex_date     DATE   NOT NULL,
    purpose     TEXT   NOT NULL,
    face_value  NUMERIC(18, 4),
    source      TEXT   NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol_id, ex_date, purpose)
);

-- Adjustment factor derived by comparing raw and adjusted closes on the same
-- date. Stored as first-class data so any historical computation is
-- reconstructible (report section 7, reproducibility).
CREATE TABLE IF NOT EXISTS adjustment_factors (
    symbol_id  BIGINT NOT NULL REFERENCES symbols (symbol_id),
    trade_date DATE   NOT NULL,
    factor     NUMERIC(18, 8) NOT NULL,
    PRIMARY KEY (symbol_id, trade_date),
    CONSTRAINT adjustment_factor_positive CHECK (factor > 0)
);

-- Ingestion bookkeeping: what makes the pipeline resumable and idempotent.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL,
    commit_sha  TEXT,
    notes       TEXT
);

-- One row per (source, data_date). The pipeline consults this to decide what
-- still needs loading, so a re-run does no duplicate work and a missed day is
-- caught up automatically.
--   status 'loaded'   data present and stored
--   status 'no_data'  not a trading day (archive returned 404)
--   status 'failed'   attempted and failed; will be retried next run
CREATE TABLE IF NOT EXISTS ingestion_log (
    source      TEXT NOT NULL,
    data_date   DATE NOT NULL,
    status      TEXT NOT NULL,
    rows_loaded INTEGER NOT NULL DEFAULT 0,
    run_id      BIGINT REFERENCES ingestion_runs (run_id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, data_date),
    CONSTRAINT ingestion_log_status CHECK (status IN ('loaded', 'no_data', 'failed'))
);

CREATE TABLE IF NOT EXISTS integrity_findings (
    finding_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     BIGINT REFERENCES ingestion_runs (run_id),
    check_name TEXT NOT NULL,
    severity   TEXT NOT NULL,
    symbol_id  BIGINT REFERENCES symbols (symbol_id),
    trade_date DATE,
    detail     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT integrity_severity CHECK (severity IN ('info', 'warning', 'error'))
);
CREATE INDEX IF NOT EXISTS integrity_findings_run_idx ON integrity_findings (run_id);
