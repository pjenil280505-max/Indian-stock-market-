# Phase 0 — Discovery Report

**Project:** Automated Indian equity research & daily candidate-selection system
**Phase:** 0 (discovery and architecture only — no strategy, no backtest, no trading)
**Date of investigation:** 2026-09-22
**Status:** `MODIFY` — proceed, but with two corrections to the assumed design (see §0.2)

---

## 0. Executive summary

### 0.1 Headline recommendation

| Decision | Recommendation | Cost |
|---|---|---|
| **Primary daily data** | NSE official UDiFF Bhavcopy + `sec_bhavdata_full` (delivery data) | ₹0 |
| **Adjusted history / backfill** | Upstox Developer API v3 historical candles (authenticated) | ₹0 (see §2.6 risk) |
| **Corporate actions** | NSE `corporates-corporateActions` endpoint, cross-checked against price-ratio detection | ₹0 |
| **Universe source** | NSE `EQUITY_L.csv` (2,580 rows, 2,317 in `EQ` series — measured) | ₹0 |
| **Compute / scheduler** | GitHub Actions scheduled workflows (no always-on server) | ₹0 |
| **Database** | Neon serverless Postgres (free tier) | ₹0 |
| **Notifications** | Telegram Bot API, outbound HTTPS from the scheduled job | ₹0 |
| **Android control** | GitHub mobile app `workflow_dispatch` + Telegram for monitoring | ₹0 |
| **Total** | | **₹0 / month** |

**Estimated monthly cost for Phases 1–3: ₹0.** First unavoidable cost arrives only if (a) the
Upstox free-API window closes (§2.6), or (b) database size exceeds the 0.5 GB free tier (§3.3).
Both have concrete, costed fallbacks below.

### 0.2 The two corrections to the assumed design

The brief assumed a fairly conventional "cloud app" shape. Two findings from this audit change it:

**Correction 1 — you do not need a hosted application at all, and you should not have one.**
The brief's requirements ("runs in the cloud", "runs when I am offline", "background processing",
"persistent database", "Telegram bot") read like a request for an always-on server. For a
once-per-day batch research pipeline, an always-on server is the wrong shape: it is the single
most expensive component, the one most likely to be silently down, and it buys nothing, because
the workload is a ~10-minute burst after market close. A scheduled CI job plus a serverless
database delivers identical behaviour at zero cost with no sleep/wake problem, no restart
problem, and no server to patch. The architecture in §1 has **no long-running process anywhere.**

**Correction 2 — broker APIs cannot be the backbone of an unattended system, and this is the
single most important constraint in the project.** SEBI-mandated 2FA means broker access tokens
expire daily (typically invalidated between 01:00 and 06:00 IST), so every broker-API-based
design requires either a manual login each morning or storing your trading password and TOTP
seed in the cloud. The second option puts live-trading credentials in a CI secret store for a
system whose stated purpose explicitly excludes trading. That is an unacceptable risk/benefit
trade, and it is the reason the *primary* data path in §2 is the exchange's own public archive
files, which need no authentication, never expire, and are the authoritative source anyway.

### 0.3 Verdict

`MODIFY` — not `PASS`, because two items in the brief should be changed before Phase 1:

1. The implied always-on cloud service should be dropped in favour of scheduled jobs (§1).
2. Broker APIs should be demoted from primary data source to optional enrichment (§2).

And not `BLOCKED`, because nothing discovered prevents the project. A zero-cost, legally
defensible, technically verified data and compute path exists today.

---

## 0.4 What was actually verified (not assumed)

Every claim below was executed live from the Phase 0 container on 2026-09-22 and is
reproducible via `scripts/verify_sources.py`.

| # | Check | Result |
|---|---|---|
| 1 | Environment | Python 3.11.15, `git`, `curl`, `psql`, `docker` present; **no** pandas/numpy installed |
| 2 | NSE UDiFF Bhavcopy, 2026-09-21 / 09-18 / 09-17 | **HTTP 200**, 206,771 / 204,944 / 203,981 bytes |
| 3 | NSE `sec_bhavdata_full` 2026-09-18 | **HTTP 200**, 397,444 bytes — includes `DELIV_QTY`, `DELIV_PER` |
| 4 | NSE `EQUITY_L.csv` | **HTTP 200**, 182,228 bytes, 2,580 rows: 2,317 `EQ`, 236 `BE`, 27 `BZ` |
| 5 | NSE root page `www.nseindia.com/` | **HTTP 403** — page blocks non-browser clients, archives do not |
| 6 | NSE corporate actions API | **HTTP 200**, 5,946 bytes, ex-dates + ISIN + face value |
| 7 | BSE Bhavcopy, 2026-09-21 / 09-18 | **HTTP 200**, 884,578 / 850,429 bytes, includes ISIN + `TckrSymb` |
| 8 | Upstox v3 daily candles **without a token** | **HTTP 200**, real OHLCV returned (see §2.6 — undocumented) |
| 9 | Upstox daily history depth | Confirmed back to **2000-01-03** |
| 10 | Upstox 1-minute intraday, 2026-09-18 | **HTTP 200**, **375 candles** (full session) |
| 11 | Upstox max window per request (`days/1`) | 5y **OK** (1,417 candles); 10y/15y/20y → **HTTP 400** `UDAPI1148` |
| 12 | **Corporate-action adjustment** | **Upstox is back-adjusted; NSE bhavcopy is raw** — proven, §2.4 |
| 13 | Upstox User-Agent filtering | `Python-urllib/3.11` → **403**; browser UA and honest custom UA → **200** |
| 14 | Upstox throttling | 15 rapid requests, 0 failures, ~4.8 req/s — no throttle at that rate |
| 15 | `api.telegram.org` reachability | **HTTP 401** to a fake token — correct rejection, network path confirmed |
| 16 | Yahoo Finance `RELIANCE.NS` | **HTTP 429 Too Many Requests** — unusable |
| 17 | Stooq CSV | JavaScript browser-verification challenge — unusable |
| 18 | Storage sizing | Computed from the real universe count, §3.3 |

Checks 16 and 17 matter as much as the successes: the two most commonly recommended "free"
sources for this kind of project both failed on a first, polite, single request. Any design
resting on them would be broken on day one.

---

## 1. Recommended cloud architecture

### 1.1 Shape

```
                      ┌─────────────────────────────────────────┐
                      │  GitHub Actions (scheduled, ~18:30 IST) │
                      │  ── no always-on process anywhere ──    │
                      └──────────────────┬──────────────────────┘
                                         │
     ┌───────────────────────────────┬───┴────────────┬────────────────────────┐
     ▼                               ▼                ▼                        ▼
 NSE archives                   Upstox v3        Neon Postgres           Telegram Bot API
 (bhavcopy, delivery,           (adjusted        (universe, bars,        (outbound HTTPS
  corp actions, EQUITY_L)        history)         features, signals,      push only)
  no auth required                                predictions, logs)
                                         │
                                         ▼
                            Android: GitHub mobile app
                            (workflow_dispatch = manual run)
                            Telegram (read reports)
```

### 1.2 Why GitHub Actions rather than a host

The workload is: wake once daily after the close, download a few hundred KB, compute, write to
Postgres, send a Telegram message, exit. Total runtime ~5–15 minutes. Characteristics that make
CI the right fit:

- **No sleep/wake problem.** There is nothing to keep warm. A cron-triggered job has no cold
  start that affects correctness, unlike a free web host that spins down.
- **Free tier fits with a 6× margin.** Private repos get 2,000 Linux minutes/month on the Free
  plan. A 10-minute daily job over ~21 Indian trading days = **~210 minutes/month**.
- **Secrets management is built in** (encrypted repository secrets), so no credential store to run.
- **Execution logs, run history and alerting on failure are built in** — this would otherwise be
  a component to build.
- **Android control is free.** The GitHub mobile app can trigger a `workflow_dispatch` run and
  read logs, which covers "controllable from Android" with no extra infrastructure.
- **Reproducibility.** Each run is pinned to a commit SHA, which directly serves the brief's
  "every candidate must be reproducible" requirement.

### 1.3 Known limitations of GitHub Actions (must be designed around)

| Limitation | Impact | Mitigation |
|---|---|---|
| Scheduled runs are **delayed under load**, commonly 15–30 min, occasionally skipped | A job pinned to the market close may run late or not at all | Schedule well after the close (18:30 IST, not 15:35); make the job **idempotent** and able to catch up on missed days from the DB's last-loaded date |
| Cron is **UTC only** | IST = UTC+5:30; no DST in India, so the offset is stable | Store `cron: '0 13 * * 1-5'` (18:30 IST) and document the conversion |
| **Scheduled workflows auto-disable after 60 days without commits** (documented for public repos; treat as applying here) | Silent total system stoppage | The daily job commits a heartbeat/state file, or a monthly keepalive workflow re-enables; **plus** a Telegram "no report received" watchdog (§4.4) |
| Minimum schedule interval 5 min | Irrelevant for a daily job | None needed |
| 6-hour max job runtime | Full historical backfill is ~3.5 h (§2.7) — fits, but tightly | Run the initial backfill in **chunked, resumable** batches, not one job |
| 2,000 min/month on Free, private repo | Fine at 210 min/month; breaks if intraday polling is added later | Keep intraday work to batch pulls; revisit if minute-bar collection is added |

**Caveat flagged honestly:** one secondary source suggested `schedule` events may be restricted
on free *private* repositories. I could **not** confirm this in GitHub's official documentation,
and the official billing docs do state Free-plan private repos receive included runner minutes.
This is listed as unresolved question **U1** (§13) and must be settled by a 10-minute empirical
test at the start of Phase 1 — it is cheap to test and would change the compute choice.

### 1.4 Alternatives considered

| Option | CPU/RAM | Storage | Sleep/restart behaviour | Scheduled jobs | Est. cost | Production-suitable later? |
|---|---|---|---|---|---|---|
| **GitHub Actions** (recommended) | 4 vCPU / 16 GB per run | Ephemeral; artifacts 500 MB | N/A — no process to sleep | Native `schedule`, UTC, 15–30 min jitter | **₹0** (210 of 2,000 min) | Yes for batch research; no for realtime |
| Oracle Cloud Always Free (ARM Ampere) | 4 OCPU / 24 GB | 200 GB block | Always on; **idle instances can be reclaimed** on the free tier | OS cron | ₹0 | Yes, but it is a VPS — patching, monitoring and uptime become your job |
| Google Cloud Run + Cloud Scheduler | Configurable | Ephemeral | Scales to zero; cold start | Cloud Scheduler | ₹0–₹80 | Yes — the best paid-upgrade path |
| Render (free web service) | 0.1 CPU / 512 MB | Ephemeral | **Spins down after inactivity**, ~50 s cold start | Cron jobs are a paid feature | ₹0 free / ~₹600+ | Weak — free tier spin-down is disqualifying |
| Railway | Usage-based | Volumes | Always on | Native cron | Trial credit, then ~₹420+ | Yes, but not free |
| Fly.io | Configurable | Volumes | Auto-stop/start | External trigger needed | ~₹0–₹400 | Yes |
| Cloudflare Workers | 10 ms CPU (free) | KV / D1 | N/A | Native cron triggers | ₹0 | **No** for the research engine — the 10 ms CPU limit and no-Python constraint rule it out. Viable **only** for the Telegram webhook (§4.3) |
| Windows VPS | — | — | — | — | — | Explicitly excluded by the brief |

**Rejected:** Render free tier (spin-down + paid cron), Heroku (no meaningful free tier),
anything requiring a laptop or Windows VPS.

**Runner-up:** Google Cloud Run + Cloud Scheduler. If Actions' `schedule` proves unreliable in
practice, this is the migration target; the code is unchanged because the job is a plain Python
entry point, not a web app. **Design implication: keep the pipeline a CLI-invokable Python
module with no web framework, so the compute host stays swappable.**

---

## 2. Indian market data — the critical Phase 0 question

### 2.1 Recommended minimum-cost suitable starting source

**A two-source design, not one source:**

1. **NSE official archive files — the authoritative primary.** Daily raw OHLCV for the entire
   market in a single ~206 KB file, no authentication, no rate limit, no token expiry, and it is
   the exchange's own published record. Verified working (checks 2–4, 6).
2. **Upstox Developer API v3 — corporate-action-adjusted history for research.** Verified back
   to 2000-01-03, with 1-minute intraday available from Jan 2022 (checks 9–11).

The split exists because of finding §2.4: these two sources answer *different* questions, and
conflating them would silently corrupt every backtest.

### 2.2 Why not "just use a broker API"

Because of the daily-token problem (§0.2, Correction 2). To restate the cost precisely: making
any broker API work unattended requires storing your trading account password **and** your TOTP
seed where a scheduled job can read them. That converts a research repository into a credential
store for a live brokerage account. The project brief forbids broker execution entirely, so this
risk purchases nothing. NSE archive files need no credentials at all.

### 2.3 Provider comparison

Every row was checked against the provider's own documentation or tested live. `—` means not
documented publicly and would require a sales enquiry.

#### Recommended

**1. NSE official archives** (`nsearchives.nseindia.com`) — **PRIMARY**

| Attribute | Finding |
|---|---|
| Price / free tier | ₹0, no account, no key |
| API limits | None published; files are static HTTP objects. Root domain 403s non-browser clients (check 5), archive paths do not |
| Historical depth | Bhavcopy archives go back many years; practical depth to be confirmed in Phase 1 (**U2**) |
| Realtime / delayed | **End-of-day only.** Not a realtime source |
| NSE coverage | Complete — all series (2,317 `EQ` + 236 `BE` + 27 `BZ`, measured) |
| BSE coverage | None (use BSE's own bhavcopy, below) |
| Corporate actions | Yes — dedicated endpoint verified (check 6): ex-date, ISIN, face value, purpose |
| **Unique value** | **`DELIV_QTY` / `DELIV_PER` — delivery-volume percentage.** Not available from any broker API tested. A genuine India-specific feature for distinguishing real accumulation from intraday churn |
| Authentication | None |
| Reliability concerns | Occasional archive-path renames (the UDiFF format migration changed URLs); needs a defensive loader with a clear failure alert |
| Commercial / research use | **Restricted — see §11.** Personal research is defensible; redistribution is not |
| Automated daily collection | One file per day is proportionate and low-impact. Must be rate-limited, identified by a custom User-Agent, and never parallelised aggressively |
| Adjusted prices | **No — raw/unadjusted** (proven, §2.4) |

**2. Upstox Developer API v3** — **SECONDARY (adjusted history + intraday)**

| Attribute | Finding |
|---|---|
| Price / free tier | ₹0 currently. **Material risk — see §2.6** |
| API limits (authenticated, documented) | **50 req/s, 500 req/min, 2,000 req/30 min** for historical candles. No per-day limit documented |
| Max window per request | **≤5 years for `days/1`** — 10y+ returns HTTP 400 `UDAPI1148` (measured, check 11) |
| Historical depth | **Daily/weekly/monthly from Jan 2000** (verified to 2000-01-03). **Minutes/hours from Jan 2022** |
| Realtime / delayed | Realtime quotes and websocket available with authentication |
| NSE coverage | Full NSE equity via `NSE_EQ|<ISIN>` instrument keys |
| BSE coverage | Yes, `BSE_EQ` segment |
| Corporate actions | **Handled implicitly via back-adjustment** (proven, §2.4). No separate corporate-action feed |
| Authentication | OAuth2 + TOTP; token expires daily. **Undocumented behaviour: the historical endpoint returned 200 with no token (check 8) — do not depend on this, see §2.6** |
| Reliability concerns | Filters by User-Agent (`Python-urllib` → 403, check 13); free-access terms may change |
| Commercial / research use | Governed by Upstox developer terms; requires an account |
| Automated daily collection | Yes, within documented rate limits |
| Adjusted prices | **Yes — back-adjusted, prices and volume** (proven, §2.4) |

**3. BSE Bhavcopy** (`bseindia.com/download/BhavCopy/Equity/`) — **OPTIONAL, for BSE coverage**

Verified HTTP 200, ~880 KB/day, same UDiFF-style schema as NSE including `ISIN`, `TckrSymb`,
`SctySrs`, OHLC, volume, turnover, trade count. ₹0, no auth. Recommended **deferred**: most
liquid Indian equities trade primarily on NSE, and `ISIN` gives a clean join key when BSE is
added later. Adding BSE early doubles the data-quality surface for little research gain.

#### Considered and rejected

| Provider | Price | Free tier | History | NSE | BSE | Corp. actions | Auth | Verdict |
|---|---|---|---|---|---|---|---|---|
| **Zerodha Kite Connect** | **₹500/mo per API key** (historical bundled since 8 Feb 2025; previously ₹2,000+₹2,000) | Personal API free but **excludes market and historical data** | Deep | Yes | Yes | Partial | OAuth + daily TOTP | **Rejected for Phase 1.** Costs ₹6,000/yr for data obtainable free; daily token expiry unsuited to unattended jobs. Strongest candidate *if* a paid broker feed is later needed |
| **Angel One SmartAPI** | ₹0 | Yes — historical free across NSE/BSE/NFO/BFO/MCX/CDS | `ONE_DAY`: 2,000 days/request; `ONE_MINUTE`: 30 days, 8,000-candle cap | Yes | Yes | — | Daily token | **Viable free alternative.** Keep as the documented fallback to Upstox (§2.6) |
| **Dhan (DhanHQ v2)** | ₹0 | Yes; ~20,000 req/day reported | ~5 years intraday minute data | Yes | Yes | — | Daily token | **Viable free alternative.** I could not load the official rate-limit page (404) or confirm daily-data adjustment behaviour, so its numbers are less firm than Upstox's |
| **Fyers / ICICI Breeze / Motilal Oswal** | Mostly ₹0 with account | Varies | Varies | Yes | Varies | — | Daily token (Motilal: expires 06:00 IST) | Not evaluated in depth; same daily-token constraint |
| **TrueData** (authorised NSE/BSE/MCX vendor) | **₹1,439.83 – ₹2,795.83/mo** per segment; tick add-on ₹299–₹999; extra symbols ₹99–₹1,998 | No | Deep | Yes | Yes (separate subscription) | Vendor-managed | **Rejected on cost.** ₹17,000–₹33,500/yr for one segment. Note: "Velocity plans do not include API access" — API requires separate approval. Legitimate licensed path if the project ever commercialises |
| **Global Datafeeds** | Not published — sales enquiry | No | Tick/min/day/week/month | Yes | Yes | Vendor-managed | Key-based | Rejected: opaque pricing, no free tier to evaluate |
| **Yahoo Finance / `yfinance`** | ₹0 | — | Deep | Partial | Partial | Claims adjustment | None | **Rejected — and empirically so.** A single polite request returned **HTTP 429** (check 16). Also: no licence for this use, unstable undocumented endpoint, known Indian-ticker data-quality issues. The most commonly recommended option and one of the worst |
| **Stooq** | ₹0 | — | Moderate | Partial | No | — | None | **Rejected empirically** — JavaScript browser-verification challenge (check 17) |
| **Alpha Vantage / Twelve Data / marketstack / EODHD / Finnhub** | Free tiers ~25–800 req/day; paid from ~$20–$80/mo | Yes, small | Varies | Thin/mid-cap gaps | Poor | Varies | API key | **Rejected for Phase 1.** Free tiers are far below a 2,317-symbol daily need, and Indian small/mid-cap coverage is the weakest part of global aggregators — exactly where this project needs accuracy |
| **Screener.in / Trendlyne / Tickertape scraping** | ₹0 | — | — | — | — | — | — | **Rejected.** Terms prohibit scraping; no licence for automated collection |

### 2.4 Critical finding: adjustment semantics (proven, not assumed)

This is the most important technical result of Phase 0 and would be very expensive to discover
in Phase 3.

Test: Reliance Industries 1:1 bonus issue, ex-date 28-Oct-2024. Compare the *same* trading day
(25-Oct-2024, pre-ex) from both sources.

| Field | NSE bhavcopy (raw) | Upstox v3 | Ratio |
|---|---|---|---|
| Open | 2687.00 | 1343.50 | **2.000000** |
| High | 2688.70 | 1344.35 | **2.000000** |
| Low | 2644.00 | 1322.00 | **2.000000** |
| Close | 2655.70 | 1327.85 | **2.000000** |
| Volume | 9,298,748 | 18,597,496 | **0.500000** |

Exact to every decimal place on all five fields.

**Conclusions:**
1. **Upstox daily candles are back-adjusted** for corporate actions — prices divided, volume
   multiplied by the adjustment factor.
2. **NSE bhavcopy is raw/unadjusted** — it is the as-traded record.
3. **Turnover is therefore the only adjustment-invariant liquidity measure.** Adjusted *volume*
   is inflated by past corporate actions (here, doubled), so a liquidity filter written on
   adjusted volume is silently wrong for every stock that has ever split or issued a bonus.
   **Liquidity filters must use turnover (₹), or raw volume from bhavcopy — never adjusted
   volume.** This single rule prevents a whole class of invisible backtest bias.
4. **A cross-source consistency check is now available for free:** the ratio between the two
   sources on any day should be the cumulative adjustment factor and nothing else. Any
   unexplained deviation is a data-quality alarm. This should be a scheduled data-integrity test
   in Phase 1, not a manual check.

### 2.5 Derived design rules

- Store **both** raw (bhavcopy) and adjusted (Upstox) series. Never overwrite one with the other.
- Persist the adjustment factor per symbol per day as first-class data, so any historical
  computation is reconstructible — required by the brief's reproducibility rule.
- Signals and indicators → **adjusted** series (continuity matters).
- Liquidity, position sizing, tradability → **raw** prices and **turnover**.
- Delivery percentage → bhavcopy only.
- Join on **ISIN**, not ticker. Indian tickers are renamed (verified: BSE and NSE files both
  carry ISIN; `EQUITY_L.csv` carries `ISIN NUMBER`). Ticker-based joins are a known
  survivorship-bias vector.

### 2.6 Risks that must be stated plainly

**Risk A — the unauthenticated Upstox endpoint is undocumented behaviour, not a feature.**
The official documentation states `Authorization: Bearer {token}` is required. It returned
HTTP 200 without any token (check 8). This is either an intentional public endpoint that is
under-documented, or an oversight. **It may be closed without notice.** Therefore:
- Phase 1 must implement the **documented, authenticated** path as the supported route.
- The no-token path may be used for convenience during development only.
- Nothing in the architecture may *depend* on it.
- The system must alert, not silently degrade, if it starts returning 401/403.

**Risk B — Upstox free API access may be time-limited.** A secondary source stated free access
to Upstox trading and market-data APIs is "valid till 30th September 2026" — i.e. **eight days
from this report's date**. **I could not confirm this on the official Upstox announcements
page**, which showed no such notice. It is therefore recorded as unverified, but it is
high-impact and cheap to check, so it is unresolved question **U3** and should be confirmed
directly with Upstox *before* Phase 2 commits to them.
*Mitigation already in the design:* the primary data path (NSE archives) has no such exposure.
If Upstox becomes paid, the documented fallbacks are Angel One SmartAPI (free, NSE+BSE
historical) or Dhan, and the abstraction in §8 makes this a single-adapter change.

**Risk C — User-Agent filtering.** `Python-urllib/3.11` is rejected with 403; a browser UA and
an honest custom UA (`indian-stock-research/0.1`) both succeed (check 13). **Use the honest
custom User-Agent with contact information, not a spoofed browser string.** Impersonating a
browser to evade filtering is both fragile and the wrong posture toward a data provider.

### 2.7 Measured collection budget

| Operation | Requests | Time | Note |
|---|---|---|---|
| Daily incremental (all NSE) | **1 file** | **~2 s** | 206 KB bhavcopy — the whole market in one request |
| Daily incremental via broker API | 2,317 | ~35 min | 2,000 req/30 min — **1,000× worse than the bhavcopy.** Confirms bhavcopy as primary |
| Full 26y adjusted backfill | ~13,900 (6 × 2,317, given the 5-year window cap) | **~3.5 h** | Must be chunked and resumable; do not attempt in one CI job |
| Delivery data daily | 1 file | ~2 s | 397 KB |

---

## 3. Database recommendation

### 3.1 Recommendation: Neon (serverless PostgreSQL), free tier

PostgreSQL as requested — it is genuinely the right choice here, not merely the familiar one:
window functions for time-series features, `NUMERIC` for exact price arithmetic, real
constraints to enforce data integrity, and a migration path to TimescaleDB if bar volume grows.

### 3.2 Neon vs Supabase — the deciding factor

| | Neon | Supabase |
|---|---|---|
| Free storage | 0.5 GB | 500 MB DB + 1 GB file |
| Idle behaviour | **Autosuspend after ~5 min idle; resumes on next query, typically <500 ms** | **Project paused after 1 week of inactivity** |
| Scale-to-zero | All plans | Free tier only |
| Extras | Database branching | Auth, REST, storage, realtime |

**Neon wins on exactly the axis that matters for this workload.** A daily batch job is, by
definition, idle 99.3% of the time. Neon's autosuspend/resume is transparent to a job that
connects once a day. Supabase's one-week inactivity *pause* is an active hazard: a holiday week,
a paused project, and the daily job fails — precisely the "runs while I am offline" requirement
the brief calls out. Neon's free tier degrades on volume; Supabase's degrades on time. This
project has low volume and long idle gaps.

Supabase's bundled auth/REST/realtime are genuinely useful features, but for a system whose only
interface is a Telegram message they are unused weight.

### 3.3 Storage sizing (computed from the measured universe)

| Scope | Rows | Est. Postgres size | Fits 0.5 GB free? |
|---|---|---|---|
| 5y × 1,200 liquid symbols | 1,500,000 | **~0.14 GB** | **Yes, comfortably** |
| 10y × 1,200 liquid symbols | 3,000,000 | **~0.29 GB** | **Yes** |
| 26y × all 2,317 `EQ` | 15,060,500 | **~1.45 GB** | **No — ~3× over** |

**Recommendation: start with 10 years × a liquidity-filtered universe (~0.29 GB).** Ten years
spans the 2016 demonetisation, the 2018 mid-cap drawdown, the 2020 COVID crash and recovery, and
the 2021–22 regime shift — enough distinct regimes for the regime-dependent testing in §6, with
room inside the free tier.

**Explicitly deferred, with the trade recorded:** minute bars are far larger (~375 bars/symbol/day,
check 10) and will not fit a free tier at universe scale. When intraday research begins, either
(a) store minute bars only for a small watchlist, or (b) keep them as compressed Parquet in
object storage and load on demand. Do not silently start writing minute bars to the primary DB.

**Cost if exceeded:** Neon paid tiers start around **$19/month (~₹1,600)**. Alternatives:
Supabase Pro ~$25/mo, or Parquet-in-object-storage at near-zero cost for cold history. The
architecture should keep bar storage behind a repository interface so this swap is contained.

### 3.4 Alternatives considered

| Option | Verdict |
|---|---|
| **Neon** | **Recommended** — best idle semantics for a daily batch job |
| Supabase | Strong second; one-week inactivity pause is the disqualifier |
| Railway Postgres | Good, but not free after trial credit |
| SQLite in the repository | Tempting and genuinely simplest, but **rejected**: concurrent access from CI is awkward, a multi-hundred-MB binary in git is pathological, and it forecloses the SQL features the research engine needs |
| Turso / libSQL | Good free tier, but SQLite dialect gives up Postgres window-function ergonomics |
| Aiven / ElephantSQL | ElephantSQL is discontinued; Aiven's free Postgres is limited |

---

## 4. Telegram architecture

### 4.1 Design: outbound push, no bot process

Network path verified (check 15). The reporting component needs **no hosted bot**: a scheduled
job makes an HTTPS POST to `api.telegram.org/bot<token>/sendMessage` at the end of the pipeline.
There is nothing to keep running and nothing to restart.

### 4.2 Message set for Phase 1 (send-only)

1. **Daily market summary** — index levels, breadth, detected regime label, sector leaders/laggards.
2. **Candidate stocks** — ranked shortlist with the reason each qualified.
3. **Setup information** — hypothesis family, feature values that triggered it, liquidity check result.
4. **Entry / SL / target** — **only when a validated setup exists**, and only with its
   out-of-sample evidence attached. Until validation exists, the report explicitly says so.
5. **Rejection / no-trade** — when nothing qualifies, say nothing qualified and why. A silent day
   must be distinguishable from a broken system.
6. **Data-integrity alerts** — failed downloads, adjustment-ratio anomalies, stale universe.
7. *(Later)* **Outcome tracking** — realised results of previously reported candidates.

**Every message carries the git commit SHA and run ID** that produced it. That is what makes a
recommendation reproducible rather than merely recorded.

### 4.3 Two-way control (deferred to Phase 4, deliberately)

The brief asks for Android control. Ranked by cost:

1. **GitHub mobile app `workflow_dispatch`** — ₹0, no new infrastructure, gives manual re-run,
   log access and failure notifications. **Sufficient for Phase 1–3; recommended.**
2. **Telegram webhook on Cloudflare Workers** — ₹0 (1M req/day free, native cron). Needed only
   for conversational commands (`/status`, `/rerun`, `/why RELIANCE`). Note the free tier's
   10 ms CPU limit: the Worker must only *enqueue* work (e.g. dispatch a GitHub workflow), never
   compute. It is a thin relay, not a backend.
3. **`getUpdates` polling from the scheduled job** — simplest, but latency equals the schedule
   interval. Adequate for low-urgency commands.

**Hard constraint carried forward: Telegram is read-only with respect to trading. No order
placement, no broker interaction, ever. `/rerun` is the most privileged command permitted.**

### 4.4 Watchdog

An absent message is the most likely failure mode (job disabled per §1.3, scheduler skip, token
expiry). Mitigation: the job sends a report **every** trading day even when the shortlist is
empty, so silence is itself the alarm. A Phase 2 addition: a separate weekly workflow that checks
the DB's last-loaded date and escalates if stale.

---

## 5. AI — deliberately absent, with boundaries set now

No AI API is integrated in Phase 0, per the brief. Recording the boundary now prevents scope creep
later.

### 5.1 Where AI could later add genuine value

| Use | Why it fits |
|---|---|
| **News and announcement context** | Parsing NSE corporate announcements, earnings text and regulatory filings is genuine natural-language work with no quantitative substitute |
| **Regime narrative** | Turning a computed regime label into readable commentary for the Telegram report |
| **Hypothesis generation** | Proposing *candidate* hypotheses for the engine to test — valuable precisely because the engine, not the AI, decides |
| **Report readability** | Converting structured output into clear prose |
| **Code and research assistance** | Development-time only (Claude Pro, as already in use) — not a runtime dependency |
| **Anomaly triage** | Explaining *why* a data-integrity alert fired |

### 5.2 Where AI must NOT be used

| Prohibited | Reason |
|---|---|
| **Generating buy/sell decisions** | Not reproducible, not backtestable, not auditable. Fails the brief's reproducibility requirement outright |
| **Ranking or scoring candidates** | Ranking must be a deterministic function of stored features. An LLM in the ranker means the same inputs can yield different outputs |
| **Setting entry/SL/target levels** | Must derive from tested rules with measured cost assumptions |
| **Deciding whether a hypothesis is valid** | That is what out-of-sample statistics are for. An LLM asked "is this strategy good?" will tend to agree |
| **Any path where an LLM sees test-set results before a rule is locked** | This is the multiple-testing and post-hoc-rationalisation failure mode the brief explicitly forbids, in its most seductive form |
| **Interpreting price/volume data directly** | LLMs are poor numerical estimators; this is exactly what the quantitative engine is for |

### 5.3 Non-negotiable design rule

**The quantitative engine must run to completion, end to end, with every AI component removed or
failing.** AI is an optional enrichment layer that can only *add* commentary to a report the
engine already produced. It must never sit on the critical path. Enforce this with a CI test that
runs the full pipeline with AI disabled — the default configuration.

---

## 6. Research framework design

### 6.1 Position on the hypothesis families

The brief lists twelve families (momentum, trend following, pullbacks, breakouts, failed
breakouts, mean reversion, volatility expansion, volume expansion, relative strength, sector
rotation, gap behaviour, regime-dependent behaviour) and instructs that none be assumed
profitable. Correct instruction — and the honest prior is stronger than "don't assume":

**Most of these, tested properly on Indian equities with realistic costs, will not survive.**
That is the expected outcome, not a failure of the project. A framework that cannot return "no
hypothesis survived validation" is not a research framework; it is a machine for producing
false positives. The primary Phase 1 deliverable is therefore **the ability to reject**, and the
system must be able to report an empty shortlist indefinitely without that being treated as a bug.

### 6.2 Required framework properties (all deferred to Phase 3, designed now)

| Requirement | Implementation approach |
|---|---|
| **Train/validation/test separation** | Chronological only — never random splits on time series. Proposal: train ≤2019, validation 2020–2022, test 2023+ |
| **Locked out-of-sample data** | The test period must be **mechanically** inaccessible: a separate table/credential or a loader that refuses test dates unless an explicit unlock flag is set, with every unlock logged. Discipline alone is insufficient — this is the rule that is hardest to keep and easiest to break by accident |
| **Transaction costs** | Full Indian stack, modelled explicitly: brokerage, STT, exchange transaction charges, SEBI turnover fee, stamp duty, GST, DP charges on sells. Must be a single audited cost module, because getting STT wrong flatters every short-horizon result |
| **Slippage** | Conservative and size-dependent; a function of the stock's turnover and spread, not a flat constant. Flat slippage systematically flatters small/mid-caps, which is where spurious results concentrate |
| **Survivorship bias** | Build a **point-in-time universe** from dated `EQUITY_L.csv`/bhavcopy snapshots, including delisted symbols. Start archiving these daily **from Phase 1**, because this data cannot be reconstructed retroactively — this is the one bias control with a hard deadline |
| **Corporate actions** | Solved: §2.4 gives a verified adjusted series plus a raw series and a cross-source ratio check |
| **Lookahead-bias prevention** | Every feature carries an explicit as-of timestamp; a feature may only use data with timestamp ≤ decision time. Enforce with automated tests that deliberately feed future data and assert the feature does not change |
| **Multiple-testing controls** | Log **every** hypothesis tested, including abandoned ones, in the database before results are seen. Apply a deflated Sharpe / false-discovery-rate correction against the true trial count. Without a complete trial registry, the correction is meaningless — which is why logging must be enforced by the harness, not by good intentions |
| **Parameter robustness** | Results must hold across a *neighbourhood* of parameters, not at a point. A strategy that works at lookback 20 but not 18 or 22 is curve-fitted |
| **Market regimes** | Report performance conditioned on regime (trend/range, high/low volatility, breadth). A strategy profitable only in one regime is a regime bet and must be labelled as such |
| **Liquidity constraints** | Position size capped as a fraction of median turnover (₹, not adjusted volume — §2.4). Exclude illiquid and price-banded series (`BE`/`BZ` require separate treatment) |

### 6.3 Structural safeguard

Feature computation, hypothesis definition, and evaluation must be **separate modules with a
one-way dependency**: the evaluator may read hypotheses, but a hypothesis definition may not
read evaluation results. This makes "quietly changing the rule after seeing the result"
require an obvious, reviewable code change rather than an inline edit.

---

## 7. Safety constraints (carried into every later phase)

The system must **never**:

| Prohibition | Enforcement mechanism |
|---|---|
| Place live trades | No broker execution dependency in the repository. CI check: fail the build if an order-placement import or endpoint appears |
| Connect to a broker for execution | Data-read adapters only; no order modules |
| Use leverage | Not modelled; position sizing capped at available notional |
| Claim guaranteed profits | Report templates carry explicit statistical-uncertainty language; no "will profit" phrasing anywhere. A candidate is "statistically supported", never "profitable" |
| Select stocks merely for recent performance | Every candidate must cite a **validated** detector; recent strength alone cannot qualify one |
| Optimise until a backtest looks profitable | Complete trial registry + multiple-testing correction (§6.2) |
| Silently change rules after seeing test results | Hypotheses versioned in git and in the DB with a lock timestamp predating any test-set access |
| Use future information | As-of timestamps on all features + adversarial lookahead tests (§6.2) |

**Reproducibility requirement:** every candidate stored with the commit SHA, run ID, input data
date range, feature values, detector version, and cost assumptions used — enough to regenerate
it exactly.

---

## 8. Proposed repository architecture

The brief's proposed structure is close. Three modifications, each for a specific reason found
in this audit:

```
indian-stock-research/
├── config/                 # YAML config; secrets ONLY via env vars
├── src/
│   ├── market/             # Trading calendar, sessions, holidays, regime classification
│   ├── data/
│   │   ├── sources/        # One adapter per provider (nse_archives, upstox, bse, angelone)
│   │   ├── loaders/         # Raw ingest -> staging
│   │   └── integrity/      # ADDED: cross-source checks, adjustment-ratio validation
│   ├── database/           # Schema, migrations, repository interfaces
│   ├── universe/           # ADDED: point-in-time universe + delisting archive
│   ├── features/           # ADDED: as-of-timestamped feature computation
│   ├── research/           # Hypothesis definitions (no access to evaluation results)
│   ├── backtest/           # Engine + the Indian cost/slippage model
│   ├── validation/         # Train/val/test gate, trial registry, multiple-testing correction
│   ├── scanner/            # Runs validated detectors over the current universe
│   ├── reports/            # Report construction (pure functions, no I/O)
│   └── notifications/      # Telegram client
├── tests/
│   ├── unit/
│   ├── integration/        # Network-marked, skippable offline
│   └── bias/               # ADDED: lookahead + survivorship regression tests
├── scripts/                # Operational entry points (verify_sources, backfill, daily_run)
├── docs/
├── reports/                # Generated output (gitignored except samples)
└── .github/workflows/      # Scheduled daily job, CI, keepalive
```

**Modifications and their justification:**

1. **`src/` package root** — makes the pipeline importable and testable as a package, and keeps
   the compute host swappable (§1.4). Flat top-level directories make imports fragile in CI.
2. **`data/integrity/` added** — §2.4 produced a free, powerful cross-source validation signal.
   Data-quality checking deserves to be a first-class module, not scattered assertions.
3. **`universe/` and `features/` split out** — point-in-time universe construction (survivorship
   bias) and as-of feature computation (lookahead bias) are the two highest-risk correctness
   areas in the whole system. Burying them inside `data/` or `research/` guarantees they get less
   scrutiny than they need.
4. **`tests/bias/` added** — bias controls that are not tested are aspirations.

`reports/` is kept as the brief proposed (generated artefacts), and `research/`, `scanner/`,
`backtest/`, `validation/`, `market/`, `database/`, `notifications/`, `config/`, `tests/`, `docs/`
all retain their proposed meaning.

---

## 9. Automation design (implementation deferred)

Target daily sequence, ~18:30 IST, single scheduled workflow:

```
 1. Resolve trading calendar        -> exit early if not a trading day
 2. Update market data              -> NSE bhavcopy + delivery (idempotent; catch up missed days)
 3. Update universe                 -> EQUITY_L snapshot; archive point-in-time membership
 4. Data integrity checks           -> cross-source adjustment ratios; alert and HALT on failure
 5. Compute features                -> as-of timestamped
 6. Market / sector conditions      -> regime label, breadth, sector relative strength
 7. Run validated detectors         -> ONLY hypotheses that passed out-of-sample validation
 8. Rank candidates                 -> deterministic function of stored features
 9. Risk / liquidity filters        -> turnover-based caps; exclude illiquid and banned series
10. Generate report                 -> pure function of stored state
11. Send Telegram                   -> including explicit "no candidates" when empty
12. Store predictions               -> with commit SHA, run ID, feature snapshot
13. (later) Evaluate past outcomes  -> paper-tracking of previously reported candidates
```

**Design rules:** step 4 **halts** the pipeline rather than proceeding on bad data — a wrong
report is worse than no report. Steps 1–4 must be idempotent and resumable so a delayed or
skipped scheduler run self-heals (§1.3). Step 7 runs **zero** detectors until Phase 3 completes,
so early reports are market summaries with an explicit "no validated detectors exist yet" notice.

---

## 10. Estimated monthly cost

| Component | Phase 1–3 | If free tiers outgrown |
|---|---|---|
| Compute (GitHub Actions) | **₹0** (~210 of 2,000 min) | Cloud Run ~₹80 |
| Database (Neon) | **₹0** (~0.29 of 0.5 GB) | Neon paid ~$19 ≈ ₹1,600 |
| Market data (NSE archives) | **₹0** | ₹0 — official, no tier |
| Adjusted history (Upstox) | **₹0** | Angel One/Dhan ₹0, or Kite Connect ₹500 |
| Telegram | **₹0** | ₹0 |
| Android control | **₹0** | ₹0 |
| AI API | **₹0** (excluded) | Usage-based, optional |
| **Total** | **₹0 / month** | **₹1,600–₹2,200 / month worst case** |

**Nothing was purchased. No account was created. No deployment was made.**

---

## 11. Legal and licensing considerations

**This section is a technical risk assessment, not legal advice. Independent advice is required
before any commercial use or redistribution.**

| Consideration | Finding |
|---|---|
| **NSE data policy** | NSE's Data Usage and Sharing Policy restricts redistribution: subscribers "shall not be permitted to redistribute any Market Data" except under a relevant agreement, and users agree not to "sell/license/reproduce/distribute or otherwise provide the Data to any third party". A **non-display usage policy** exists separately and is directly relevant to algorithmic consumption of market data |
| **Personal research vs commercial** | Downloading published EOD files for personal research is materially different from redistributing them or building a commercial product. The former is defensible; **the latter requires a licence** |
| **What this means concretely** | ✅ Store data privately, compute features, generate reports for your own use. ❌ Publish a data feed, share raw data, offer signals as a paid product, or commit bulk raw data to a public repository |
| **Repository visibility** | The repository is currently **private** (verified). **Keep it private.** A public repo containing bulk NSE/BSE data is a redistribution question you do not want |
| **Broker API terms** | Each broker's developer terms govern automated access. Automating login by storing password + TOTP seed may breach the account agreement independently of the security risk (§0.2) |
| **Scraping vs published files** | Fetching NSE's own published archive files is not the same as scraping a portal. Screener.in/Trendlyne/Tickertape scraping was rejected partly on these grounds. Note `www.nseindia.com/` returns 403 to non-browser clients (check 5) — **that is a signal about intent; do not evade it with spoofed headers** (§2.6 Risk C) |
| **Rate-limiting courtesy** | One file per day is proportionate. Aggressive parallel fetching invites blocking and is indefensible if questioned |
| **Investment advice regulation** | A system producing stock recommendations could engage SEBI Investment Adviser / Research Analyst regulation **if provided to others**. For personal use it does not. **If this is ever shared, take advice first.** This is the highest-consequence legal item in the project |
| **No profit claims** | Already a hard constraint (§7), and also the correct legal posture |

---

## 12. Security considerations

| Area | Requirement |
|---|---|
| **Secrets** | Only GitHub encrypted secrets / environment variables. No credentials in code, config files, or commits. A pre-commit secret scan should be added in Phase 1 |
| **Broker credentials** | **Do not store a trading password or TOTP seed anywhere in this system.** This is the direct consequence of §0.2 Correction 2 and the reason for the NSE-primary design |
| **Telegram bot token** | Treated as a secret; token compromise allows message spoofing into your reporting channel. Rotate if exposed |
| **Telegram chat allowlist** | Reports must go only to an allowlisted chat ID. A bot that answers anyone leaks your research |
| **Database credentials** | Least privilege — the daily job needs no schema-drop rights. Separate migration and runtime roles |
| **Database exposure** | Free-tier Postgres is internet-reachable; require TLS and restrict access where the provider permits |
| **No inbound surface** | The recommended architecture has **no public endpoint at all** in Phase 1–3, which removes most of the attack surface by construction. Adding the Cloudflare Workers webhook (§4.3) introduces the first one — it must verify Telegram's secret token and allowlist the chat ID |
| **Dependency supply chain** | Pin dependencies with hashes; enable Dependabot. A compromised package in a repo holding financial data and tokens is a real risk |
| **CI permissions** | Workflow `permissions:` set to the minimum; `GITHUB_TOKEN` scoped read-only where possible |
| **Audit trail** | Every run and every stored recommendation carries a commit SHA and run ID (§7) — a security control as much as a reproducibility one |

---

## 13. Known limitations and unresolved questions

### Limitations of this Phase 0 audit
- Live checks were single-shot on one date (2026-09-22) from one network location. Availability over
  weeks, and behind an Indian consumer ISP, is unproven.
- NSE archive **depth** was verified for recent dates only; how far back the UDiFF-format files
  extend was not established.
- No account was created with any provider, so authenticated rate limits and terms are from
  documentation, not experience.
- Data **accuracy** was spot-checked on one corporate action for one symbol. The result was exact
  to six decimal places on five fields, which is strong evidence of the mechanism, but it is one
  event, not a survey.
- Neon and Supabase free tiers were assessed from documentation; neither was provisioned.

### Unresolved questions

| # | Question | Why it matters | How to resolve |
|---|---|---|---|
| **U1** | Do `schedule` events work on **free private** repositories? | Determines whether the recommended compute host works at all | 10-minute empirical test at the start of Phase 1: commit a 5-minute cron workflow and observe. **Do this first** |
| **U2** | How far back do NSE UDiFF bhavcopy archives extend, and where is the format break? | Sets the achievable raw-history depth and the point-in-time universe start date | Probe archive URLs backwards by year |
| **U3** | Is Upstox API access free beyond 30 Sep 2026? | ₹0 vs a monthly fee; a secondary source claims expiry in 8 days, unconfirmed officially | Ask Upstox developer support directly, before Phase 2 |
| **U4** | Is the unauthenticated Upstox historical endpoint intentional? | If not, it may close without notice | Implement the authenticated path regardless (already the plan); ask support |
| **U5** | Does the delivery-data (`sec_bhavdata_full`) archive have comparable depth to bhavcopy? | Delivery % is a distinctive India-specific feature; its history depth bounds its research use | Probe archive dates backwards |
| **U6** | Which series beyond `EQ` should be eligible? | 236 `BE` + 27 `BZ` symbols carry price bands and different liquidity; wrong inclusion adds noise or bias | Decide explicitly in Phase 2 universe rules, with the reasoning recorded |
| **U7** | Do NSE's non-display-usage provisions apply to personal algorithmic research? | The main legal uncertainty | Read the non-display policy in full; take advice before any sharing or commercialisation |
| **U8** | Is the NSE corporate-actions endpoint sufficient for a full adjustment history, or only forward-looking? | Determines whether adjustment factors can be reconstructed independently of Upstox | Compare its output against detected price-ratio jumps over several years |

---

## 14. Proposed future development phases

Each phase ends with a reviewable deliverable and an explicit go/no-go.

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **0** | This report + minimal scaffolding | **Awaiting your review** |
| **1** | Data foundation: DB schema, NSE + Upstox adapters, integrity checks, point-in-time universe archiving, daily scheduled job, Telegram "data loaded" report. **Resolve U1 first.** | 10 years of validated history loaded; daily job runs unattended for 10 consecutive trading days; integrity checks pass; **universe archiving live** (its data cannot be backfilled) |
| **2** | Feature engineering + market/sector/regime classification, with as-of correctness tests and the full Indian cost model | Features reproducible from stored data; **adversarial lookahead tests pass**; cost model reconciled against a real contract note |
| **3** | Research engine: hypothesis registry, backtester, train/val/test gate with a mechanically locked test set, multiple-testing correction | Framework demonstrably **rejects** a deliberately planted spurious hypothesis. This is the acceptance test that matters |
| **4** | Hypothesis testing across the twelve families. **Expected outcome: most fail.** | Every family tested and documented, survivors or not. "Nothing survived" is a valid, reportable result |
| **5** | Scanner + daily shortlist — only if Phase 4 produced validated detectors | Candidates reproducible; reports carry statistical context, never profit claims |
| **6** | Paper-trading outcome tracking; realised vs expected performance | Tracking runs unattended; divergence from backtest expectations is surfaced, not hidden |
| **7** | *Optional* AI enrichment (news/context/commentary) | Pipeline still passes all tests with AI disabled |
| **8** | *Optional, far future* broker connection | **Out of scope for this project. Requires explicit separate authorisation.** |

**Recommended next step: Phase 1, starting with U1.** Do not begin strategy work. The highest-value
and most time-sensitive Phase 1 item is **point-in-time universe archiving**, because every day it
is not running is a day of survivorship-bias data permanently lost.

---

## 15. Reproducing this report's findings

```bash
python3 scripts/verify_sources.py          # live checks against all sources
python3 -m pytest tests/ -q                # offline logic tests
```

`scripts/verify_sources.py` re-runs the connectivity, adjustment-semantics, history-depth and
universe-count checks in §0.4 and prints a pass/fail table. It is read-only, uses an honest
User-Agent, and makes a small number of requests.
