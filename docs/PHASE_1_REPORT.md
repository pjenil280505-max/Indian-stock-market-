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

---

# Addendum B — Cloud deployment verified against Neon (2026-09-23)

**Status: PASS.** The pipeline now runs in GitHub Actions against Neon, unattended.
All figures below are read from real workflow logs, not local runs.

## B.1 The blocker was GitHub Actions billing, not code

Every workflow run had been failing in 2–4 seconds with `runner_id: 0`, zero steps and no
log file — including CI runs from before any secret existed, and CI never reads
`DATABASE_URL`. GitHub was creating the job and then refusing to allocate a runner. Nothing
in the repository could affect that.

Making the repository public (public repos get unlimited free Actions minutes) resolved it
immediately: runner `1000000849` was assigned within 4 seconds and the job ran to completion.

Before publishing, the full git history was scanned: no credentials, no data files, no
`.env`. Two pattern hits were benign — the CI service container's throwaway
`postgres:postgres@localhost`, and the literal strings `"neon.tech"` / `"sslmode=require"`
used as detection logic. All six commits were then rewritten to replace the author's
personal email with the GitHub `noreply` address; file trees were byte-identical before and
after (`a823fc0`), and 162 tests passed on the rewritten tree.

## B.2 Cloud verification

| Check | Evidence from the run log |
|---|---|
| Neon connection | `target: Neon (serverless PostgreSQL)` |
| Server version | `PostgreSQL 18.6 (6569466) on aarch64-unknown-linux-gnu` |
| Schema | `schema: applied (idempotent - safe to re-run)` |
| Data written | 17,971 raw bars across 7 trading days on the first run |
| Secret exposure | `DATABASE_URL: ***` — masked by GitHub; no host, user or password in any log |
| Integrity | `findings=0 (errors=0)`, `dates_failed=[]`, `halted=False` |

**Neon runs PostgreSQL 18.6, not 16.** The schema applied cleanly on both, so no change was
needed — but the Phase 1 assumption of "Postgres 16" was wrong and is corrected here.

**GitHub runners see no NSE throttling.** The 403s that forced the jittered backoff in this
project's development container did not occur once from GitHub's network. The retry logic
never engaged. It stays in place for exactly the environments where it does.

## B.3 Idempotency, proven twice in the cloud

Daily pipeline, two identical runs three minutes apart:

| | Run 1 | Run 2 |
|---|---|---|
| `dates_loaded` | 7 dates | `[]` |
| `raw_bars_written` | 17,971 | **0** |
| Data-step duration | 9 s | 2 s |

Backfill, re-run after completion: `daily_bars_raw` unchanged at 1,599,991 and database size
unchanged at 339.8 MB. The backfill step completed in **0 seconds** — nothing outstanding.

## B.4 Three-year backfill

Range 2023-09-01 to 2026-09-22, run via the `Historical backfill` workflow.

| Metric | Value |
|---|---|
| Runtime | **13 min 43 s** (budget was 240 min) |
| Rows written | **1,599,991** daily bars |
| Dates processed | **799** ingestion-log entries = all 798 weekdays + the universe snapshot |
| Failed dates | **0** |
| Integrity findings | **0** |
| Database size | **339.8 MB (68.0% of the 0.5 GB free tier)** |
| Headroom | **160 MB** |

The storage guard (425 MB) was never reached. Actual usage came in below the 394 MB
projection, because the listed universe was smaller in 2023 than today.

## B.5 The constraint that now matters most

At 160 MB headroom and roughly 189 MB/year of ongoing growth — about 126 MB/year of daily
bars plus 63 MB/year of universe snapshots — **the free tier fills in approximately 10
months of unattended daily operation.**

The avoidable half of that is `universe_snapshots`, which writes 2,583 rows every day to
record membership that changes only a few times a month. Storing membership intervals
(`symbol_id, series, valid_from, valid_to`) instead of daily rows would cut it by well over
99% and extend the runway to several years. That is a schema change, so it was not made
unilaterally; it is the single highest-value item available and should be decided before
the headroom is spent.

Options, in order of cost:
1. **Compact `universe_snapshots` to change-intervals** — free, removes ~63 MB/year, and
   makes point-in-time reconstruction cheaper rather than harder.
2. **Trim the stored universe for bars** — exclude perpetually illiquid symbols from daily
   bar storage. Reduces research scope, so it is a real trade.
3. **Neon paid tier** — roughly $19/month. Not required yet.

## B.6 Scheduled operation

`cron: '0 13 * * 1-5'` — 13:00 UTC = **18:30 IST**, Monday to Friday, 180 minutes after the
15:30 IST close, off the top of the hour to avoid GitHub's peak scheduling delay. The
workflow is on the default branch, which is what makes the `schedule` event fire at all.
It has not yet been observed firing on its own; the next opportunity is the coming weekday
at 13:00 UTC.

## B.7 Corrected record

Two Phase 1 statements are superseded:

1. "Verified against a real PostgreSQL 16.13 server, which Neon is" — Neon is **18.6**. The
   verification still transfers, but the version was assumed rather than checked.
2. CI had been failing since 2026-09-22 and the first Phase 1 report did not mention it.
   That was an omission: a red CI pipeline should have been reported at the time rather
   than discovered two days later while debugging something else.

---

# Addendum C — universe_snapshots compacted to intervals (2026-09-23)

**Status: done and live on Neon.** The growth constraint from §B.5 is resolved.

## C.1 What changed

`universe_snapshots(snapshot_date, symbol_id, series, listing_date)` — one row per symbol
per day — is replaced by:

```sql
universe_membership(membership_id, symbol_id, series, valid_from, valid_to, listing_date)
```

`valid_to` is the **last date the symbol was observed**, never NULL. Each daily run extends
it for symbols still present; a symbol that disappears simply stops being extended, so its
interval is already closed at its true last-seen date. That removes the ambiguity a nullable
`valid_to` would create between "still listed" and "the pipeline stopped running", and needs
no separate delisting sweep.

Point-in-time reconstruction is `valid_from <= D AND valid_to >= D` — one indexed range
scan, cheaper than the old per-date equality lookup.

## C.2 The four rules, and the bias each one guards

| Rule | Why it exists |
|---|---|
| Extension compares against the **previous observation date**, not against "yesterday" | Weekends and market holidays would otherwise split every interval |
| A **gap** since the last interval opens a **new** interval | Extending across a gap would falsely claim the symbol was listed throughout — a survivorship-bias bug |
| A **series change** opens a new interval under the new series | `EQ → BE` is a real change in tradability, not a continuation |
| Re-running for the same date is a **no-op** | Scheduled runs are delayed, retried and caught up |

## C.3 Migration safety

Universe membership is the survivorship-bias control and cannot be rebuilt if lost, so
`scripts/migrate_universe.py` is deliberately cautious:

1. Collapse snapshots into intervals with a gaps-and-islands query.
2. **Verify**: for every observed date, the set of `(symbol, series)` returned by the
   interval table must exactly equal the snapshot rows — checked with a symmetric `EXCEPT`
   in both directions.
3. Drop the old table **only** with an explicit `--drop-old`, and **only** if verification
   passed.

Tested locally on a seeded fixture covering the hard cases — always-present, delist and
relist, series change, late listing — which collapsed 31 snapshot rows into exactly the 6
expected intervals:

```
A  EQ  2026-09-14 .. 2026-09-23     (present throughout)
B  EQ  2026-09-14 .. 2026-09-15     (delisted)
B  EQ  2026-09-18 .. 2026-09-23     (relisted - separate interval)
C  EQ  2026-09-14 .. 2026-09-18     (series change)
C  BE  2026-09-19 .. 2026-09-23
D  EQ  2026-09-21 .. 2026-09-23     (listed late)
```

## C.4 Applied to Neon

| Step | Result |
|---|---|
| Build intervals | 2,583 rows built from 2,583 snapshot rows |
| Verification | **PASSED** — "interval table reproduces every snapshot exactly" |
| Drop old table | Done; `universe_snapshots` no longer present |
| Daily pipeline after migration | **Clean run** — `findings=0 (errors=0)`, `halted=False` |
| Database size | 339.8 MB, unchanged |

**Honest scope of the saving: this reclaimed essentially nothing today.** The Neon table held
a single day of snapshots, so 2,583 rows became 2,583 intervals — a 0% reduction. The entire
benefit is prospective.

## C.5 The growth picture now

| | Before | After |
|---|---|---|
| Universe rows per day | ~2,583 | ~0 (extends existing rows in place) |
| Universe growth | ~63 MB/year | **~0.1 MB/year** (only genuine listing changes) |
| Total growth | ~189 MB/year | **~126 MB/year** (daily bars only) |
| Free-tier runway from 160 MB | ~10 months | **~15 months** |

Daily bars are now the only meaningful growth, and they are irreducible without dropping
history or narrowing the universe. Verified locally: 60 consecutive daily runs against a
stable universe produced **1** interval row, not 60.

## C.6 Tests

172 pass, including ten new interval-semantics tests covering consecutive-day extension,
weekend gaps, same-date re-runs, delist-and-relist, series change, absence during a gap,
previous-observation tracking, storage scaling with changes rather than days, and the
range constraint.

---

# Addendum D — Same-day reporting and schedule timing (2026-09-24)

**Status: fixed and deployed.** The report now covers the session that just closed.

## D.1 The cron was not the problem

The request was to move the schedule earlier so the daily report lands on time. It would
not have worked. `outstanding_dates()` ended its window at `today - 1`:

```python
end = today - timedelta(days=1)   # today deliberately excluded
```

So the 18:30 IST run loaded data through **yesterday**. The system was structurally one
session behind, and no cron time changes that. The original reasoning — "a run fetching the
current day would race publication" — was sound but solved the wrong problem: it traded a
manageable timing risk for a permanent one-day lag.

## D.2 What changed

**1. The current trading day is now attempted.** `end = today`.

**2. A missing archive file is classified, not assumed.** Previously any 404 was recorded as
`no_data`, and `no_data` settles forever. Attempting the current day would therefore have
marked every not-yet-published trading session as a permanent market holiday — silently
losing it, because settled dates are never re-fetched. Now:

| Condition | Status | Behaviour |
|---|---|---|
| 404, within 3 days | `failed` | retried next run |
| 404, older than 3 days | `no_data` | genuine holiday, learned once |

**3. Partial loads are revisited.** NSE publishes the bhavcopy well before
`sec_bhavdata_full`, so an early run can capture prices but no `DELIV_PER`. Those bars are
now stored and the date marked `partial`, which does not settle; a later run enriches them.
The upsert already `COALESCE`s the delivery columns, so nothing is lost either way. Beyond
the grace window a partial day is accepted as final rather than retrying forever.

`partial` required widening the `ingestion_log` status constraint. `CREATE TABLE IF NOT
EXISTS` leaves existing constraints alone, so `schema.sql` now carries an idempotent
`DROP CONSTRAINT IF EXISTS` / `ADD CONSTRAINT` pair that upgrades databases in place.

## D.3 The schedule

| | Old | New |
|---|---|---|
| Fires per day | 1 | 2 |
| First | `0 13 * * 1-5` — 18:30 IST | `41 11 * * 1-5` — **17:11 IST** |
| Second | — | `17 14 * * 1-5` — 19:47 IST |
| On the hour? | **yes** | no |

The first fire is 101 minutes after the 15:30 IST close, by which point the bhavcopy is
normally published. The second is a safety net at 257 minutes, by which point
`sec_bhavdata_full` reliably is — and it also covers a missed or heavily delayed first fire.
Both runs are idempotent, so the second costs nothing when the first succeeded.

**Neither fires on the hour.** The previous schedule used minute 0 and was observed firing
**4h 48m late** on 2026-09-23 (cron 13:00 UTC, actual 17:47:39 UTC). GitHub's own guidance
is that the top of the hour is the worst minute to pick; the Phase 0 report said so and the
original schedule then ignored it.

## D.4 Verified against live data

| Case | Result |
|---|---|
| Run dated 2026-09-23, a real trading day | **loaded 2,578 bars for 2026-09-23 itself** — the date the old code skipped |
| Run dated 2026-09-24 at 09:46 IST, before the close | **`dates_pending_publication=['2026-09-24']`**, `ingestion_log` status `failed` — retryable |
| Same run, holiday misclassification | `dates_no_data=[]` — today was **not** recorded as a holiday |
| Integrity | `findings=0 (errors=0)`, `halted=False` |

The second case is the one that matters: under the previous logic, attempting the current
day before publication would have settled it as `no_data` and lost that session permanently.

## D.5 Tests

196 pass, including a new suite covering same-day inclusion, the grace-window boundary in
both directions, delivery-completeness classification, retry-then-load once NSE publishes,
and the cron schedule itself — that neither fire is on the hour, both are after the close,
and the two are far enough apart for the safety net to enrich a partial load.

Three pre-existing tests asserted the old behaviour and were rewritten. One is worth noting:
`test_rerun_is_idempotent` compared total fetch counts, which conflated "a loaded date was
re-fetched" with "a pending date was legitimately retried". It now asserts on the loaded
date specifically, which is what idempotency actually means here.

---

# Addendum E — Measured NSE publication lag and scheduling reliability (2026-09-24)

Two numbers were measured rather than assumed: when NSE actually publishes, and whether
GitHub actually fires the schedule on time. The first came out fine. The second did not.

## E.1 NSE publication lag, measured by polling

Probed both archive paths every 5 minutes from 20 minutes after the 15:30 IST close,
retrying 403s (throttling) inside each sample so a throttle did not waste a slot.

```
10:21:30  bhavcopy=404  delivery=403     21 min after close
10:56:19  bhavcopy=404  delivery=404     last confirmed miss for prices
11:06:15  bhavcopy=200  delivery=403     PRICES LIVE
11:26:56  bhavcopy=200  delivery=404     delivery still missing at 87 min
11:31:48                delivery=200     DELIVERY LIVE
```

| File | Published after close | IST |
|---|---|---|
| Bhavcopy (prices) | **56–66 min** | ~16:31 |
| `sec_bhavdata_full` (delivery) | **87–92 min** | ~17:02–17:07 |

Delivery lands roughly **26 minutes after** prices. That gap is the entire justification for
the `partial` status added in Addendum D: a run between the two captures prices with no
`DELIV_PER`.

**Verdict on the 17:11 IST first fire:** it clears delivery publication by only **9–14
minutes**. That is the earliest defensible time — anything earlier systematically produces
`partial` days — but it is a thin margin measured on a single session, and publication
timing varies with settlement load, expiry and month-end. Slow days will produce `partial`,
which the 19:47 IST run then enriches. No data is at risk; only timeliness wobbles.

A prediction made before the measurement completed — that the 11:41 UTC run would come back
`partial` — was **wrong**. Delivery published 9 minutes before the fire. It was called from
an incomplete poll.

## E.2 Scheduled triggers are not reliable on this account

| Date | Cron | Actual fire | Schedule | Repo | Delay |
|---|---|---|---|---|---|
| 2026-09-23 | 13:00 UTC | 17:47:39 UTC | minute 0 | private | **288 min (4h 48m)** |
| 2026-09-24 | 11:41 UTC | had not fired by 13:06 UTC | minute 41 | public | **85+ min, pending** |

Manual `workflow_dispatch` runs on the same repository, same workflow, same runner pool
start in **2–3 seconds**, consistently. So the workflow, the secret, the runners and the
account are all fine. The `schedule` event itself is what arrives late.

**This contradicts the fix made in Addendum D.** Moving off the top of the hour was
predicted to cut the delay, on the basis of GitHub's documented guidance. Two observations
do not support that: the off-hour schedule is also badly late, and going public did not help
either. The change was not harmful — off-hour timing is still better practice, and the
second daily fire is genuinely useful — but it did not solve the problem it was made for,
and the Addendum D reasoning should be read with that correction.

**What this does and does not threaten.** Nothing about data integrity: the pipeline is
idempotent, computes outstanding dates from `ingestion_log`, and catches up whenever it
runs. A late fire costs timeliness only. What it does mean is that **"the report lands at
17:11 IST" is not achievable through `schedule` alone**, and no cron expression will make it
so.

If punctual delivery becomes a requirement, the only reliable mechanism observed here is
`workflow_dispatch`, which starts within seconds. That needs an external nudge — a trigger
outside GitHub calling the API on time — which reintroduces the always-on component Phase 0
deliberately removed. That trade belongs to a later phase and should be decided explicitly,
not slipped in. For now the honest position is that the daily job runs every weekday and
lands *eventually*, usually within a few hours of the intended time.
