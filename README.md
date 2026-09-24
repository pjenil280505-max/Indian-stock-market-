# Indian Stock Market Research System

A cloud-based, autonomous research system for Indian equities (NSE, later BSE).

Its purpose is to **discover and objectively test** trading hypotheses, and to produce a daily
shortlist of *statistically supported* stock setups with full reproducibility.

## What this is not

- **Not a trading system.** It does not connect to a broker and does not place orders.
- **Not a signal service.** It makes no profit claims. A candidate is "statistically supported",
  never "profitable".
- **Not a strategy.** No strategy exists yet, by design. The research framework is built before
  any hypothesis is tested, so that hypotheses can be *rejected* rather than fitted.

Most of the hypothesis families this system will test are expected to fail out-of-sample once
realistic Indian transaction costs are applied. The ability to report "nothing qualified" is a
feature, not a defect.

## Status: Phase 1 data foundation running; Phase 1a hardening applied

*Corrected in Phase 1a: this section previously said "Phase 0 complete — no system has been
built or deployed", which stopped being true in Phase 1.*

- **Phase 1** built and deployed the daily NSE data pipeline (GitHub Actions → Neon
  PostgreSQL), a resumable historical backfill, and point-in-time universe intervals. See
  [`docs/PHASE_1_REPORT.md`](docs/PHASE_1_REPORT.md).
- **Phase 1a** fixed the writer-lock defect, made the connection check read-only, moved
  schema changes into versioned migrations, and corrected earlier report errors
  (Addendum F).

### Schema changes

Normal jobs never run DDL. They stop with a clear message if a migration is pending.

```bash
python3 scripts/migrate.py status   # read-only
python3 scripts/migrate.py apply    # applies pending migrations; fails if protected data changes
```

In CI use **Actions → Database migrations → Run workflow** (`status` first, then `apply`).
New schema changes go in a new `src/db/migrations/NNNN_name.sql` file; an applied file must
never be edited.

## Phase 0 (discovery) summary

Phase 0 was discovery only: audit the environment, verify what data is actually available and
legally usable, and choose an architecture.

**Read [`docs/PHASE_0_REPORT.md`](docs/PHASE_0_REPORT.md) for the full findings.**

### Phase 0 outcome

| Decision | Choice | Cost |
|---|---|---|
| Primary data | NSE official archives (bhavcopy, delivery data, corporate actions) | ₹0 |
| Adjusted history | Upstox Developer API v3 (verified back to 2000) | ₹0 |
| Compute | GitHub Actions scheduled jobs — no always-on server | ₹0 |
| Database | Neon serverless PostgreSQL | ₹0 |
| Reporting | Telegram Bot API, outbound push only | ₹0 |
| Android control | GitHub mobile app (`workflow_dispatch`) + Telegram | ₹0 |
| **Total** | | **₹0 / month** |

Two findings changed the assumed design:

1. **No always-on server is needed or wanted.** A daily research pipeline is a ~10-minute burst
   after the close; a scheduled CI job plus a serverless database does the same job at zero cost
   with nothing to keep awake, patch, or restart.
2. **Broker APIs cannot be the backbone.** SEBI-mandated 2FA expires broker tokens daily, so
   unattended use would require storing a trading password and TOTP seed in the cloud — for a
   project that explicitly never trades. NSE's own published archive files need no credentials.

A third finding shapes every future backtest: **Upstox daily candles are corporate-action
back-adjusted while NSE bhavcopy is raw** — proven to six decimal places against Reliance's
1:1 bonus. Consequently *adjusted volume is inflated by past corporate actions*, so liquidity
filters must use turnover or raw volume. See report section 2.4.

## Verifying the findings

```bash
python3 scripts/verify_sources.py            # live checks against every source
python3 scripts/verify_sources.py --offline  # logic checks only, no network

pip install -r requirements-dev.txt
python3 -m pytest tests/ -q                  # offline unit tests
```

`verify_sources.py` is read-only, identifies itself with an honest User-Agent, and makes a small
number of requests.

### Uncertainties resolved (Addendum A)

Both high-impact uncertainties were verified against **official sources only**:

- **U1 — scheduled workflows on a free private repo: VERIFIED, no restriction.** GitHub's 60-day
  auto-disable is documented as applying to *public* repositories only. Free plan grants 2,000
  minutes/month for private repos; we need ~210. One actionable constraint: the workflow file
  must be on the **default branch** to fire at all.
- **U3 — Upstox v3 after 30 Sep 2026: VERIFIED, no official expiry exists.** The complete Upstox
  announcement history carries no sunset, deprecation or fee for v3 — and v3 was actively
  extended on 4 Sep 2026. The third-party expiry claim is unsubstantiated.

The check also surfaced the **Analytics Token**: a free, **read-only, 1-year** Upstox credential
that needs no static IP for historical data and *cannot place orders*. It removes the daily-login
problem and makes the "never trades" guarantee a property of the credential rather than of our
code. And a real operational finding: **NSE archives throttle intermittently with HTTP 403**, so
retry with backoff is mandatory — now implemented in `verify_sources.py`.

## Phase 1 — data foundation (complete, pending your setup)

Built and verified end to end against live NSE data and a real PostgreSQL 16 server:

- **NSE adapter** (primary): bhavcopy, delivery data, universe, corporate actions
- **Upstox Analytics Token adapter** (secondary): read-only adjusted history
- **Retry/backoff** tuned to NSE's real throttling behaviour
- **Point-in-time universe archiving** — the survivorship-bias control
- **9-table PostgreSQL schema** with idempotent upserts and OHLC constraints
- **Daily GitHub Action**, resumable and idempotent
- **152 tests passing**

Live run: **20,518 raw bars across 8 trading days, 2,580 universe symbols, 0 failures**.
Re-running wrote nothing — idempotency verified. The corporate-action check computed and
stored the Reliance bonus factor of **2.0** from live data.

See [`docs/PHASE_1_REPORT.md`](docs/PHASE_1_REPORT.md).

### Live in the cloud

Running unattended in GitHub Actions against Neon (PostgreSQL 18.6):

| | |
|---|---|
| Daily bars stored | **1,599,991** (2023-09-01 to 2026-09-22) |
| Universe | 2,583 symbols, archived point-in-time daily |
| Trading dates settled | 799, **0 failed**, **0 integrity findings** |
| Database | **339.8 MB / 0.5 GB free tier**, 160 MB headroom |
| Backfill runtime | 13 min 43 s |
| Schedule | `41 11` + `17 14` UTC = **17:11 and 19:47 IST**, Mon–Fri |

Idempotency verified in the cloud: a second identical daily run wrote **0 rows**, and
re-running the completed backfill left the row count and database size unchanged.

Universe membership is stored as **change-intervals** (`valid_from`/`valid_to`), not one row
per symbol per day — so a stable universe costs one row, not one per day. That cut growth from
~189 MB/year to ~126 MB/year and extended the free-tier runway from ~10 to ~15 months. Daily
bars are now the only meaningful growth. See `docs/PHASE_1_REPORT.md` section C.

Still optional: `UPSTOX_ANALYTICS_TOKEN` (read-only, 1-year, cannot trade). Without it,
adjusted bars and the corporate-action cross-check stay dormant; the pipeline warns and
continues.

```bash
# local development
pip install -r requirements.txt -r requirements-dev.txt
export DATABASE_URL=postgresql://...
python3 scripts/daily_update.py --dry-run          # fetch only, no writes
python3 scripts/daily_update.py                    # daily update with catch-up
python3 scripts/backfill.py --source nse --start 2016-01-01   # chunked history
python3 -m pytest tests/ -q
```

## Next phase

**Phase 2 — features and market context.** Not started; awaiting review.

Before it begins, one thing is still unproven: the scheduled job has never fired on its own.
Let it run unattended for several consecutive trading days first.

## Legal

Market data is used for **personal research only**. NSE's data policy restricts redistribution.
This repository must stay **private**, and no raw market data is committed (see `.gitignore`).
Report section 11 covers the licensing and regulatory position; it is a technical risk
assessment, not legal advice.
