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
3. **Neon paid tier** — usage-based with no monthly minimum; estimated at a few dollars a
   month at this project's size. Not required yet. *(Corrected in Phase 1a; this line
   originally said "roughly $19/month". See Addendum F.5.)*

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

*Superseded in Phase 1a: this runtime ALTER was moved into the guarded migration
`0002_ingestion_log_partial_status.sql` and `schema.sql` was retired (Addendum F.3).*

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

*Table updated in Phase 1a with the measured fire times (see Addendum F). The original
row for the 11:41 fire read "had not fired by 13:06 UTC, 85+ min, pending".*

| Date | Cron | Actual fire | Schedule | Repo | Delay |
|---|---|---|---|---|---|
| 2026-09-23 | 13:00 UTC | 17:47:39 UTC | minute 0 | private | **288 min (4h 48m)** |
| 2026-09-24 | 11:41 UTC | 15:54:50 UTC | minute 41 | public | **253 min (4h 13m)** |
| 2026-09-24 | 14:17 UTC | 18:27:03 UTC | minute 17 | public | **250 min (4h 10m)** |

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

## E.3 Follow-up: both fires arrived about 4 hours late on 2026-09-24

*Corrected in Phase 1a (Addendum F). The original version of this section was headed
"neither fire arrived", stated "zero scheduled runs occurred on 2026-09-24", and concluded
that scheduled delivery had "no useful upper bound". The first two statements were false
and the third was not supported by the evidence. The account below replaces it.*

**What was observed at the time.** At 14:41 UTC, after both fires were due, `event=schedule`
returned only the previous day's run. That observation was accurate *at 14:41 UTC*; the
error was reporting it as the day's outcome instead of waiting for the known 4–5 h delay.

**What actually happened** (GitHub Actions API, workflow `daily-data-update.yml`):

| Run | Event | Created (UTC) | Delay vs cron | Outcome |
|---|---|---|---|---|
| #7 | `workflow_dispatch` (manual fallback) | 15:44:51 | — | success; **loaded 2026-09-24, 2,577 raw bars** |
| #8 | `schedule` (11:41 cron) | 15:54:50 | **253 min** | success; nothing outstanding (no-op) |
| #9 | `schedule` (14:17 cron) | 18:27:03 | **250 min** | success; nothing outstanding (no-op) |

So both scheduled fires did arrive and ran successfully. 2026-09-24 was ingested by the
manual fallback ten minutes before the first scheduled fire; had the fallback not been
triggered, the 15:54 scheduled run would have ingested it.

**Accurate conclusion.** Across three observed scheduled fires the delay was 288, 253 and
250 minutes — consistently about four to five hours, with none skipped. That is *not* "no
upper bound", but it is also far outside the intended 17:11 IST delivery: in practice the
scheduled data lands around 21:25–22:15 IST. GitHub documents that scheduled runs can be
delayed under load and that queued runs can be dropped, so three data points are not a
guarantee either way.

**Unchanged by this correction:** data integrity. The pipeline is idempotent, computes
outstanding dates from `ingestion_log`, and a late or missed run costs only timeliness.
Same-day delivery at a fixed time still needs something other than `schedule` — a manual
trigger, or an external caller of `workflow_dispatch` — which remains an explicit
architecture decision for a later phase.

---

# Addendum F — Phase 1a corrections and hardening (2026-09-24)

Phase 1a fixed defects found by the transformation audit. It added no features and changed
no market data. Everything below is a correction of something this report or the code
previously got wrong.

## F.1 The daily job and the backfill were NOT mutually excluded

Commit `ef4e5a1` stated that the backfill workflow shared "a database-writer concurrency
group with the daily job so the two can never write concurrently". That was false: the
backfill used `group: database-writer` but the daily job used `group: daily-data-update`.
Different groups do not exclude each other, so a scheduled daily run could run during a
backfill.

**Fixed.** Every workflow that is given `DATABASE_URL` — daily update, backfill, and the new
migrations workflow — now uses `group: database-writer` with `cancel-in-progress: false`.
A test (`tests/test_phase1a_guards.py`) fails the build if any workflow with database access
uses a different group.

GitHub caveat, stated rather than hidden: a concurrency group holds at most one *pending*
run. If a second run queues while one is already pending, the older pending run is
cancelled (a *running* job is never cancelled). Every writer is idempotent and resumable,
so this can delay work but cannot lose or corrupt data.

## F.2 The connection check executed DDL

`scripts/check_connection.py` was described as a preflight check but called
`apply_schema()`, which ran `CREATE TABLE IF NOT EXISTS …` and an `ALTER TABLE … DROP/ADD
CONSTRAINT` block on every run — including before every scheduled daily update.

**Fixed.** The check now opens the connection with `read_only = True`, so PostgreSQL itself
rejects any write or DDL in its transactions. It reports migration status and exits `4` if
the schema is behind, instead of silently changing it. Tests verify both the static property
(the script requests a read-only connection and never commits) and the behaviour (running it
leaves the catalogue and all data unchanged, and on an unmigrated database it reports
`BEHIND` without creating anything).

## F.3 Schema changes moved out of normal jobs into versioned migrations

Previously `src/db/schema.sql` was re-executed at the start of every daily run, backfill and
connection check, and carried a runtime `ALTER TABLE ingestion_log DROP CONSTRAINT … ADD
CONSTRAINT …` block (added in Addendum D). Normal jobs therefore held DDL rights they used
every run.

Now:

| Piece | Role |
|---|---|
| `src/db/migrations/0001_baseline.sql` | The schema exactly as it stood (verified: `pg_dump -s` identical to the old `schema.sql` output). All `IF NOT EXISTS`, so a no-op on the existing Neon database. |
| `src/db/migrations/0002_ingestion_log_partial_status.sql` | The old runtime ALTER, guarded: it inspects the constraint and only rebuilds it if `partial` is missing. On Neon it is a no-op (verified locally: constraint OID unchanged). |
| `src/db/migrate.py` | Applies pending migrations in order, each in its own transaction, under an advisory lock; records a SHA-256 of each file in `schema_migrations` and refuses to continue if an applied file is later edited. |
| `scripts/migrate.py` | `status` (read-only) and `apply`. `apply` fingerprints `daily_bars_raw`, `universe_membership` and `symbols` (row count, range, and a sum of per-row hashes) before and after, and fails if any existing table changed. |
| `.github/workflows/migrate.yml` | The only workflow that changes schema. Manual, `database-writer` group. |
| daily update, backfill | Call `require_current_schema()`: a read-only check that stops the job if a migration is pending. They contain no DDL (enforced by test). |

The only change this makes to the production database is one new bookkeeping table,
`schema_migrations`, with one row per migration. It is reversible with
`DROP TABLE schema_migrations`, which touches no market data.

## F.4 Archived: the completed universe-interval migration

`scripts/migrate_universe.py` and `.github/workflows/migrate-universe.yml` (Addendum C) were
moved to `archive/2026-09-universe-interval-migration/`. The migration completed in
September 2026; its workflow was still dispatchable and capable of `DROP TABLE`. The files are
kept, unmodified, as the audit trail for Addendum C.

## F.5 Neon pricing was misstated

This report (B.5) and the Phase 0 report (§3.3, §10) said Neon's paid tier "starts around
$19/month". Neon's paid Launch plan is usage-based with no monthly minimum (as checked during
the transformation audit: about $0.35 per GB-month of storage and about $0.106 per CU-hour
of compute). For this project's size — a few hundred MB and short daily bursts — the
estimated bill if the free tier is outgrown is a few dollars a month, not $19. This is an
estimate; confirm on Neon's pricing page before any purchase decision.

## F.6 Other wording corrected

- `src/pipeline.py` docstring said scheduled runs are "delayed 15-30 min and can be skipped
  entirely". Measured delays on this repository are 250–288 minutes; none were skipped.
- Addendum E.2 table and E.3 corrected as marked in place.
- **CI was red on `main` from `802558f` until Phase 1a.** `tests/test_publication_timing.py`
  imported PyYAML, which CI does not install; the commits in that period reported "196 tests
  pass" from a local environment that happened to have it. The test now parses the cron
  lines with a regex and the suite is verified in a clean virtualenv built only from
  `requirements*.txt`, as CI builds it.

## F.7 Not fixed in Phase 1a, and why

- **The runtime database role can still run DDL.** Jobs no longer *do*, and the connection
  check *cannot*, but the daily update and backfill connect as the same Neon role that the
  migrator uses. Separating a no-DDL runtime role needs a new Neon role and a new secret,
  which Phase 1a was not authorised to create.
- **Scheduled delivery is still ~4–5 hours late.** Unchanged; see E.3.
- **Public-repository scheduled workflows are auto-disabled after 60 days without repository
  activity** (GitHub's documented rule, which Phase 0 noted applied to public repositories
  only; this repository is now public).

---

# Addendum G — Phase 1b: Cloudflare Cron → GitHub dispatch trigger (2026-09-24)

**Status: deployed 2026-09-25 04:05 UTC and verified end to end in TEST mode only.** Production
dispatch remains disabled. See G.6 for the live evidence.

## G.1 What was built

A Cloudflare Worker (`cloudflare/dispatcher`, name `nse-pipeline-dispatcher`) whose only job
is `POST /repos/…/actions/workflows/<file>/dispatches` to the GitHub API. The Python pipeline
stays on GitHub runners, under the unchanged `database-writer` lock. The Worker never contacts
Neon, NSE or Upstox (enforced by `tests/test_cloudflare_guards.py`).

| Invocation | Target workflow | State |
|---|---|---|
| test (cron `37 4 * * *` UTC, or the key-guarded test endpoint) | `cloudflare-dispatch-probe.yml`: no checkout, no secrets, `permissions: {}` | active once deployed |
| production | `daily-data-update.yml` (input `catchup_days`) | **disabled**: needs `PRODUCTION_ENABLED="true"` and a cron in `PRODUCTION_CRONS` |

## G.2 Why the test target is a probe and not the daily workflow

Dispatching `daily-data-update.yml` is not a no-op against Neon even when every date is
already loaded: run #10 on 2026-09-24 inserted an `ingestion_runs` row and upserted
`corporate_actions=2`. Using it as the Cloudflare test would therefore write to Neon, which
Phase 1b forbids. The probe proves the same path, from Cloudflare cron to a GitHub run
starting, with no database contact, and it measures the dispatch-to-start delay.

## G.3 Credentials (manual; never in source or chat)

| GitHub secret | Create as | Minimum scope |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare custom API token | Account → Workers Scripts: Edit, this account only |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID | — |
| `GH_DISPATCH_TOKEN` | GitHub fine-grained PAT | This repository only; Actions: Read and write |

**Limit of the GitHub token, stated plainly:** GitHub cannot scope a token to a single
workflow. `Actions: write` on this repository also allows dispatching *any* workflow
(including backfill and migrations), cancelling or re-running runs, deleting run logs, and
enabling or disabling workflows. The Worker's code allowlist limits what *the Worker* can do,
not what the token can do if it leaks.

## G.4 Verified so far

- 22 Worker unit tests, plus 18 Python guard tests. The Python suite went from 243 to 262,
  with one extra test for the `migrate.py status` fingerprint.
- Local run in Wrangler's workerd runtime, with a loopback stub standing in for GitHub. The test
  cron dispatched only the probe, with the expected path, headers and inputs. An unconfigured
  cron dispatched nothing. Wrong key gave 401 and GET gave 404. No token or key appeared in any log.
- `check-secrets` (run 36058353183): all three secrets MISSING, as expected before setup.
- Neon baseline for the Phase 1b tests (`migrate.py status`, read-only, run 36058423103):
  `daily_bars_raw` 1,605,146 rows, digest 296770750422; `universe_membership` 2,585,
  digest -39741809479; `symbols` 2,585, digest 57018452335. These are identical to the Phase 1a
  post-migration fingerprint.

## G.5 Credentials

The three repository secrets were created by the account owner on 2026-09-25. Their values
appear in no log: GitHub masks them as `***`, and Wrangler lists the Worker secrets as `(hidden)`.

## G.6 Live verification (2026-09-25)

| Step | Run | Evidence |
|---|---|---|
| Local cron test (workerd on a GitHub runner, real token) | Actions 36092911394 | Worker logged `dispatch_ok`, request `55c69ab4…`, GitHub HTTP 204 |
| → probe started | Actions 36092930316 | run-name carries `55c69ab4…`; created 04:04:22, 1 s after dispatch |
| Deploy | Actions 36092952992 | `nse-pipeline-dispatcher`, version `a5ea841c-30f1-4867-bc88-ca68a23a0d75`, trigger `schedule: 37 4 * * *`, `PRODUCTION_ENABLED ("false")` |
| Deployed Worker invoked once (test endpoint) | same run | Worker on Cloudflare returned `ok:true, kind:test, workflow:cloudflare-dispatch-probe.yml`, request `3cc6fa0d…`, GitHub HTTP 204 |
| → probe started | Actions 36092983417 | run-name carries `3cc6fa0d…`; job started 5 s after the Worker's timestamp |
| Test key rotated away | same run | old key: 204 on the non-dispatching `/__test-auth`, then 401 five seconds later |
| Public surface probed from outside | — | `GET /` 404; `POST /__test-dispatch` with no key or a wrong key 401; `/__scheduled` 404 |
| **Real Cloudflare cron fire** (`37 4 * * *`) | Actions 36095196493 | scheduled time 04:37:30.000Z (as reported by Cloudflare); run created 04:37:31; job running 04:37:36 (**36 s after the nominal cron minute**, against 250–288 min for GitHub's own `schedule`) |
| Neon after all tests | Actions 36093032212 | fingerprint identical to baseline 36058423103 (all three digests) |
| Neon after the scheduled fire | Actions 36095233036 | identical again |
| No data workflow triggered | — | the daily update's last run is still #10 (2026-09-24 20:41 UTC); the backfill's is still #2 |

**Documentation vs reality.** GitHub's REST docs now describe the dispatch endpoint as
returning 200 with `workflow_run_id`. Every live dispatch here returned **204 with no body**.
The Worker accepts both, and matches runs through the `request_id` in the probe's run-name.

**Not observed directly:** Cloudflare's own dashboard logs and cron event history. The deploy
token is deliberately limited to Workers Scripts: Edit, which cannot read logs. The account
owner can see them under Workers & Pages → nse-pipeline-dispatcher → Logs, and Settings →
Trigger Events. The Cloudflare-side evidence here is the deploy output and the deployed
Worker's own responses.

## G.7 Still running after Phase 1b

The test cron fires once a day at 04:37 UTC and dispatches only the no-op probe. It costs
about 10 s of Actions time per day and builds a daily record of Cloudflare → GitHub latency.
Production dispatch stays disabled until it is explicitly approved.

---

# Addendum H — Cloudflare → NSE connectivity test (2026-09-25)

Read-only. One GET per resource from the **deployed** Worker, and the same GETs from a
GitHub runner for comparison (Actions run 36116297925, 09:03–09:04 UTC, during market
hours). Nothing was stored, no workflow was dispatched, and Neon and Upstox were not
contacted. The production flags and `wrangler.toml` are unchanged.

| # | Resource | Cloudflare status | CF ms | Bytes | Payload check | GitHub status | GH ms |
|---|---|---|---|---|---|---|---|
| 1 | `EQUITY_L.csv` (universe) | 200 | 319 | 182,582 | CSV header OK, **2,585 rows** (= Neon `symbols`) | 200 | 665 |
| 2 | `sec_bhavdata_full_24092026.csv` | 200 | 250 | 395,867 | header incl. `DELIV_QTY, DELIV_PER`, 3,492 rows | 200 | 257 |
| 3 | UDiFF `…20260924_F_0000.csv.zip` | 200 | 355 | 203,837 | valid ZIP, entry `BhavCopy_NSE_CM_0_0_0_20260924_F_0000.csv`, CSV header decompressed | 200 | 253 |
| 4 | `api/corporates-corporateActions` | 200 | 415 | 869 | JSON array, 3 records | 200 | 270 |
| 5 | `sec_bhavdata_full_25092026.csv` (today, not yet published) | 404 | 335 | 3,533 | HTML "not found" page, correctly **not** flagged as blocked | 404 | 279 |

- Every response from both origins carried `server: cloudflare`. NSE's archives and API are
  currently served through Cloudflare, not Akamai, so the Phase 0 concern about Akamai
  challenging Worker egress did not apply to these endpoints on this date.
- No challenge page, 403, 429 or redirect occurred.
- **Caveats.** This was one sample per resource. The probe ran in Cloudflare colo **SJC**
  because the HTTP caller was a US GitHub runner; cron-triggered invocations run where
  Cloudflare places them, so their latency may differ. In Workers, `Date.now()` advances
  only on I/O, so the "headers" and "total" timings are I/O-bound measurements rather than a
  true TTFB. NSE can change its bot rules at any time, and Worker subrequests identify
  themselves through Cloudflare's `CF-Worker` header.
- A live check in the Workers runtime found a ZIP bug that the Node unit tests missed:
  workerd rejects trailing bytes after the deflate stream. It was fixed before deployment,
  and a test now uses a real archive layout.

Neon fingerprint: identical before (run 36116259457) and after (run 36116431702), matching
every Phase 1a/1b fingerprint. No data workflow ran: the daily update is still at 10 runs,
the backfill at 2, and the probe at 3.

---

# Addendum I — Readiness gate, run tagging, schedule analysis (2026-09-25)

Production remains **disabled**. This phase builds and tests what production would run.

## I.1 Readiness gate (`cloudflare/dispatcher/src/readiness.js`)

Evaluated on every production fire, cheapest check first. Nothing is dispatched unless all
the checks pass:

| Step | Check | Outcome |
|---|---|---|
| 1 | GitHub: runs of `daily-data-update.yml` for the IST trade date tagged `cloudflare <date>` | queued/running/succeeded → **skip**; ≥2 failed → **skip** (retry budget); GitHub unreadable → **skip** (fail closed) |
| 2 | NSE delivery bhavcopy | 200 + `DATE1`/`DELIV_PER` header + first row dated the trade date + ≥500 rows → **dispatch (complete)** |
| 3 | Only from `READINESS_FINAL_UTC` (14:45): UDiFF zip, first row dated right | **dispatch (partial)**; the pipeline enriches the day later |
| — | otherwise | **skip** |

NSE is not contacted once today's run exists. The trade date is the IST date of the UTC fire
time.

**Live dry run on the deployed Worker** (Actions 36133318095, version `2ec93987…`), which never
dispatches:

| Case | Delivery | UDiFF | Decision |
|---|---|---|---|
| 2026-09-25 @ 11:45 UTC | 200, 3,507 rows, `25-Sep-2026` | — | dispatch (complete) |
| 2026-09-28 @ 11:00 UTC | 404 | not checked | skip: not published yet |
| 2026-09-26 @ 14:45 UTC (Sat) | 404 | 404 | skip: not published by the final window |

The real 25-Sep delivery file is rejected when asked for 24-Sep ("file dated 25-Sep-2026").
The gate ran within the free plan's 10 ms CPU limit while validating a 397 KB file. Neon was
unchanged before (run 36133279645) and after (run 36133397610); no daily, backfill or probe run
was created.

## I.2 Run tagging

`daily-data-update.yml` now has a `run-name`, plus optional blank-default inputs `request_id` and
`trade_date` that are used only in that name and never in a shell step:

- Cloudflare: `Daily data update (cloudflare 2026-09-25 <request id>)`
- GitHub cron: `Daily data update (schedule)`
- Manual: `Daily data update (workflow_dispatch)`

The gate depends on the `cloudflare <date>` tag, so a guard test pins the format. actionlint
passes, and was confirmed to catch errors inside `run-name`. The GitHub schedule is unchanged.

## I.3 GitHub schedule: analysis and proposal (not applied)

Every observed GitHub scheduled fire was 4–5 hours late: 288, 253 and 250 minutes. None were
skipped, but GitHub documents that scheduled runs can be delayed or dropped under load. Once
Cloudflare production runs `*/15 11-14 * * 1-5` with the gate, the first fire after
publication (typically 11:45 UTC) loads the day. The GitHub fires would then arrive around
15:50 and 18:30 UTC to find nothing to do, but each still writes an `ingestion_runs` row and
upserts corporate actions.

| Option | Duplicates | Late runs | If Cloudflare fails (e.g. token expiry) |
|---|---|---|---|
| A. Keep both GitHub crons | 2 no-op runs/day | yes | covered |
| B. Remove both | none | none | **nothing runs, silently** |
| **C. Remove `41 11`, keep `17 14` as fallback, and let it exit early when today's Cloudflare run succeeded** | none in normal operation | only when needed | covered, about 4 hours late |

**Recommendation: C.** Option B makes the fine-grained token's expiry a silent single point of
failure. Option A keeps paying for duplicates. Option C keeps an independent path that doesn't
depend on the token, and costs one near-instant no-op run per day.

## I.4 Exactly what enabling production would change (not applied)

**1. `cloudflare/dispatcher/wrangler.toml`**
```diff
 [triggers]
-crons = ["37 4 * * *"]
+crons = ["37 4 * * *", "*/15 11-14 * * 1-5"]
@@ [vars]
-PRODUCTION_CRONS = ""
-PRODUCTION_ENABLED = "false"
+PRODUCTION_CRONS = "*/15 11-14 * * 1-5"
+PRODUCTION_ENABLED = "true"
```
This fires 16 times per weekday, 11:00–14:45 UTC (16:30–20:15 IST), using 2 of the free plan's 5
cron triggers. Before publication each fire makes 1 GitHub GET and 1 small NSE GET (404,
~3.5 KB). After the day's dispatch each fire makes only 1 GitHub GET. That is about 3–4 NSE
requests per day.

**2. `.github/workflows/daily-data-update.yml` (option C)**
```diff
 on:
   schedule:
-    - cron: '41 11 * * 1-5'
     - cron: '17 14 * * 1-5'
 permissions:
   contents: read
+  actions: read
 ...
+      - name: Fallback only - skip if Cloudflare already loaded today
+        id: fallback
+        if: github.event_name == 'schedule'
+        env:
+          GH_TOKEN: ${{ github.token }}
+        run: |
+          # UTC date, NOT IST: this fire lands ~18:30 UTC, already tomorrow in IST.
+          day=$(date -u +%Y-%m-%d)
+          n=$(gh api "repos/${{ github.repository }}/actions/workflows/daily-data-update.yml/runs?event=workflow_dispatch&status=success&created=>=$day" \
+                --jq "[.workflow_runs[] | select(.display_title | contains(\"cloudflare $day\"))] | length")
+          echo "skip=$([ "$n" -gt 0 ] && echo true || echo false)" >> "$GITHUB_OUTPUT"
```
Every later step would also get `if: steps.fallback.outputs.skip != 'true'`.

**3. Tests that must change in the same commit.** Each currently fails the build if
production is on or the schedule changes:
- `tests/test_cloudflare_guards.py`: `TestProductionIsOff` (3 tests) and
  `test_github_schedule_unchanged_in_this_phase`
- `tests/test_publication_timing.py`: `test_two_fires_per_trading_day` and
  `test_fires_are_separated_enough_to_enrich`
- `cloudflare/dispatcher/test/index.test.js`: "the committed wrangler.toml keeps production off"
