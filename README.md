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

## Status: Phase 0 complete — awaiting review

Phase 0 was discovery only: audit the environment, verify what data is actually available and
legally usable, and choose an architecture. **No system has been built or deployed.**

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

## Next phase

**Phase 1 — data foundation.** Database schema, source adapters, integrity checks, and the daily
scheduled job. Two items come first:

1. **Resolve unresolved question U1** (do `schedule` events work on free private repos?) — a
   10-minute test that could change the compute host.
2. **Start point-in-time universe archiving.** This is time-sensitive: every day it is not
   running is a day of survivorship-bias control data that cannot be recovered later.

Phase 1 does not begin until Phase 0 is reviewed and approved. See report section 14 for the
full phase plan.

## Legal

Market data is used for **personal research only**. NSE's data policy restricts redistribution.
This repository must stay **private**, and no raw market data is committed (see `.gitignore`).
Report section 11 covers the licensing and regulatory position; it is a technical risk
assessment, not legal advice.
