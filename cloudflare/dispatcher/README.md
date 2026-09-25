# nse-pipeline-dispatcher (Cloudflare Worker)

Cloudflare Cron → this Worker → GitHub `workflow_dispatch` → existing Python
pipeline on GitHub runners → Neon.

The Worker is **a trigger only**. It holds no data, never connects to Neon or
Upstox, and runs no pipeline code. Outbound: the GitHub API (read today's
runs; dispatch) and GETs of the allowlisted NSE files for the readiness gate.

## State: PRODUCTION (data collection only), approved in Addendum I.4

| | Setting | Effect |
|---|---|---|
| Test cron | `37 4 * * *` (04:37 UTC daily) | Dispatches the **no-op probe** workflow |
| Production cron | `*/15 11-14 * * 1-5` (11:00–14:45 UTC, weekdays) | Readiness gate, then `daily-data-update.yml` at most once per day |
| `PRODUCTION_ENABLED` | `"true"` | |
| `READINESS_FINAL_UTC` | `"14:45"` | From here a UDiFF-only day is accepted as partial |
| GitHub fallback | `17 14 * * 1-5` (14:17 UTC) in `daily-data-update.yml` | Exits immediately if today's Cloudflare run succeeded |

Invocation kinds:

- **test** → `.github/workflows/cloudflare-dispatch-probe.yml`: no secrets, no
  checkout, `permissions: {}`, and no database access. It logs the scheduled time,
  the start time and the delay between them.
- **production** → `.github/workflows/daily-data-update.yml` (inputs:
  `catchup_days`). Needs `PRODUCTION_ENABLED = "true"` **and** a matching
  cron in `PRODUCTION_CRONS`. Both are guarded by tests
  (`tests/test_cloudflare_guards.py`) so turning production on is a
  deliberate, reviewed change.

The code allowlists these two workflow files. Nothing else in the repository
(migrations, backfill) can be dispatched by this Worker.

## Credentials (created by you, stored only as GitHub repository secrets)

| Secret | What | Scope |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare **custom** API token | Account → **Workers Scripts: Edit**, this account only |
| `CLOUDFLARE_ACCOUNT_ID` | Your account ID (dashboard sidebar) | not a credential, stored as a secret by choice |
| `GH_DISPATCH_TOKEN` | GitHub **fine-grained** PAT | Only this repository; Repository permissions → **Actions: Read and write** (Metadata: Read is added automatically). Nothing else. |

The deploy workflow uploads `GH_DISPATCH_TOKEN` into the Worker as an
encrypted Worker secret. It never appears in source, `wrangler.toml` or logs.

## Operating it

Actions → **Cloudflare Worker** → Run workflow:

1. `check-secrets` prints `present` or `MISSING` for each secret, and nothing else.
2. `local-test` runs the Worker in Wrangler's local runtime on the runner and
   fires the test cron. This makes **one real dispatch** of the probe. It needs only
   `GH_DISPATCH_TOKEN`.
3. `deploy-and-test` deploys to Cloudflare, invokes the **deployed** Worker once
   through `POST /__test-dispatch` with a random key created for that run, then
   rotates the key to a value nobody holds and proves the old key is rejected.

Local development (never needs real credentials):

```bash
npm ci && npm test
npx wrangler dev --test-scheduled         # then, in another shell:
curl "http://127.0.0.1:8787/cdn-cgi/local/scheduled?cron=37+4+*+*+*"
```

## HTTP surface

Every request returns 404 except `POST /__test-dispatch` and `POST /__test-auth`.
Both exist only while `TEST_TRIGGER_KEY` is set and require it as a bearer
token. After each deploy-and-test that key is a random value no one holds.
`/__test-dispatch` can only dispatch the probe workflow, even if production is
enabled, and `/__test-auth` never dispatches.
