# Phase 1 — Data Foundation

**Status:** `MODIFY` — the data foundation is built and verified end to end, but one
objective could not be completed by me and needs ten minutes of your time (§4).

**Scope honoured:** data only. No strategy, no signals, no ranking, no backtest, no AI,
no broker execution, nothing deployed, nothing purchased.

---

## 1. What was built

```
src/
  config.py            settings + tuned retry/pacing constants (env vars only, no secrets in repo)
  http_client.py       GET-only client, jittered exponential backoff
  integrity.py         corporate-action, duplicate, missing-data and sanity checks
  pipeline.py          resumable, idempotent daily orchestration
  sources/nse.py       PRIMARY: bhavcopy, delivery data, universe, corporate actions
  sources/upstox.py    SECONDARY: Analytics Token adapter for adjusted history
  db/schema.sql        9 tables
  db/repository.py     idempotent upserts; no delete/truncate anywhere
scripts/
  daily_update.py      daily entry point
  backfill.py          chunked, resumable historical loader
  verify_sources.py    Phase 0 source checks (retained)
.github/workflows/
  daily-data-update.yml  13:00 UTC = 18:30 IST, Mon-Fri
  ci.yml                 tests against a real PostgreSQL 16 service
```

No pandas, no requests, no broker SDK. Parsers use only `csv`, `zipfile`, `json` and
`urllib`, so CI installs in seconds and the supply-chain surface is one package
(`psycopg`).

## 2. Verified by execution, not assertion

### 2.1 Live end-to-end run (real NSE → real PostgreSQL 16.13)

```
universe_symbols=2580
dates_loaded=[2026-09-10, 09-11, 09-14, 09-15, 09-16, 09-17, 09-18, 09-21]
dates_failed=[]          dates_no_data=[]
raw_bars_written=20518   corporate_actions=20
findings=0 (errors=0)    halted=False
```

Stored coverage, queried back out of the database:

| trade_date | bars | with delivery data |
|---|---|---|
| 2026-09-10 | 2559 | 2289 |
| 2026-09-11 | 2560 | 2287 |
| 2026-09-14 | 2560 | 2287 |
| 2026-09-15 | 2561 | 2302 |
| 2026-09-16 | 2564 | 2302 |
| 2026-09-17 | 2566 | 2301 |
| 2026-09-18 | 2573 | 2299 |
| 2026-09-21 | 2575 | 2315 |

Plus a backfill of 2024-10-24/25 (3,849 bars) for the corporate-action test below.

### 2.2 Idempotency — verified by re-running

A second immediate run: `dates_loaded=[] raw_bars_written=0`. No date was re-fetched.
Settled dates are skipped via `ingestion_log`, so a delayed or duplicated scheduler run
costs nothing and corrupts nothing.

### 2.3 The corporate-action check, proven on live data

Live NSE raw and live Upstox adjusted bars loaded for Reliance around the 1:1 bonus
(ex-date 28-Oct-2024), then the integrity check run against the database:

```
2024-10-24: paired=1 findings=0  ADJUSTMENT FACTOR symbol_id=1862 -> 2.0
2024-10-25: paired=1 findings=0  ADJUSTMENT FACTOR symbol_id=1862 -> 2.0
```

Factor 2.0 computed from live data and persisted, with no false alarm, and the volume
consistency check passed — confirming the Phase 0 finding that adjusted volume is
inflated. The rule it enforces: **liquidity filters must use turnover or raw volume,
never adjusted volume.**

### 2.4 Tests

**152 passed.** Offline (no database): 129 passed, 23 skipped.

| Area | Tests |
|---|---|
| Retry/backoff | 21 |
| Parsers (real NSE/Upstox shapes) | 23 |
| Integrity: corporate actions, duplicates, missing data, sanity | 30 |
| Database: idempotency, point-in-time universe, constraints | 23 |
| Pipeline: resumability, catch-up, halting | 16 |
| Read-only enforcement | 25 |
| Phase 0 source checks | 14 |

## 3. Findings from Phase 1 itself

**Retrying too fast renews NSE's throttle.** The first live run failed: a 2/4/8/16s
backoff ladder exhausted itself while the block was still active — yet the very next
request, 28 seconds later, succeeded. Faster retries were making it worse. Fixed by
raising the floor to 5s, extending to 5 attempts (~155s total budget), adding ±25%
jitter, and pacing NSE requests 1s apart. The subsequent run loaded eight days with zero
failures. A test now asserts the retry budget stays above 120s so this cannot silently
regress.

**The OHLC CHECK constraint caught bad test data of mine** before it reached a table —
defence in depth working as intended.

## 4. What I could not complete, and why

**Neon PostgreSQL is not provisioned.** I have no Neon account and was instructed not to
purchase anything, so I could not create the instance or hold its credentials. This is
the one Phase 1 objective not finished, and it is why the status is `MODIFY`.

What I did instead: verified every database guarantee against a **real PostgreSQL 16.13
server** — schema, constraints, idempotent upserts, point-in-time universe
reconstruction, ingestion-log resumability. Neon is ordinary PostgreSQL, so this
transfers. The remaining work is genuinely yours and takes about ten minutes:

1. Create a free Neon project and copy the connection string.
2. Add it as the repository secret `DATABASE_URL`.
3. Run the `Daily data update` workflow once via `workflow_dispatch` to create the schema
   and load the first snapshot.

**Upstox Analytics Token is not configured.** Generating it requires signing in to your
Upstox account. The adapter is written and was exercised live against the real v3
endpoint (§2.3). Add the token as the secret `UPSTOX_ANALYTICS_TOKEN` when convenient;
the pipeline runs without it and logs a warning, it does not fail.

**Historical depth is currently shallow by choice.** 8 recent trading days plus 2
backfilled days. Deep history is a deliberate, separate operation:
`scripts/backfill.py --source nse --start 2016-01-01` in batches. It was not run here
because it belongs against your real database, not a throwaway local one.

## 5. Known limitations

| Limitation | Status |
|---|---|
| Neon not provisioned; secrets not set | Needs you (§4) |
| Scheduled workflow never observed firing | Cannot be tested without a live schedule; it is on `main` now, which is the documented requirement |
| Adjusted bars not loaded in the daily job | Deliberate: full backfill is ~3.5 h and belongs in `backfill.py`, not a scheduled job |
| Corporate-actions endpoint is forward-looking | Cannot rebuild a full adjustment history from it alone; the raw-vs-adjusted ratio is the independent cross-check (report question U8) |
| Delivery data covers ~89% of bars | Expected: not every series reports delivery |
| `BE`/`BZ` series archived but not yet classified as eligible | Deliberate; question U6, decided in Phase 2 |
| NSE archive depth still unprobed | Question U2, unchanged |
| Upstox adapter exercised without a token | The endpoint answered unauthenticated (Phase 0 check 8). The supported path is the Analytics Token; wiring is proven, the token path is not yet exercised with a real token |

## 6. Recommendation for Phase 2

**Do not start feature engineering yet.** Two things should happen first, in order:

1. **Connect Neon and let the daily job run unattended for ten consecutive trading
   days.** Point-in-time universe archiving only has value if it is actually running;
   every day it is not is a day of survivorship-bias data permanently lost. Ten clean
   days also proves the scheduler behaves, which nothing so far has demonstrated.
2. **Backfill 10 years of NSE raw history** in batches via `scripts/backfill.py`, then
   adjusted history for the liquid universe. Expect roughly 0.29 GB, inside Neon's
   0.5 GB free tier for ~1,200 symbols × 10 years.

Then Phase 2 proper: features with as-of timestamps, the market/sector/regime layer, and
the full Indian transaction-cost model — with the adversarial lookahead tests written
*before* the features they guard.
