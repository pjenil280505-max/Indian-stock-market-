// Cloudflare Cron -> GitHub Actions workflow_dispatch.
//
// This Worker is a trigger and nothing else. It holds no market data, never
// talks to Neon or NSE, and runs no pipeline code. Its only outbound request
// is a single POST to the GitHub API asking GitHub to start a workflow; the
// existing Python pipeline then runs on GitHub's runners exactly as before,
// under the same `database-writer` concurrency lock.
//
// Two kinds of invocation, kept strictly apart:
//   test        -> cloudflare-dispatch-probe.yml, a no-op workflow with no
//                  secrets and no database access.
//   production  -> daily-data-update.yml. DISABLED: it requires both
//                  PRODUCTION_ENABLED = "true" and a cron listed in
//                  PRODUCTION_CRONS, and neither is set in Phase 1b.
//
// All times are UTC. Cloudflare evaluates cron expressions in UTC and
// `scheduledTime` is a UTC epoch; it is logged as an ISO-8601 "Z" string.

export const WORKFLOWS = Object.freeze({
  test: "cloudflare-dispatch-probe.yml",
  production: "daily-data-update.yml",
});

const GITHUB_API = "https://api.github.com";
const USER_AGENT = "nse-pipeline-dispatcher";
const MAX_ERROR_CHARS = 200;

function list(value) {
  return String(value ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Decide what a cron fire means. Pure; no I/O. */
export function classify(cron, env) {
  if (list(env.PRODUCTION_CRONS).includes(cron)) {
    if (env.PRODUCTION_ENABLED === "true") {
      return { kind: "production" };
    }
    return { kind: "blocked", reason: "production dispatch is disabled" };
  }
  if (list(env.TEST_CRONS).includes(cron)) {
    return { kind: "test" };
  }
  return { kind: "ignored", reason: "cron not configured for any invocation kind" };
}

/** The exact request to send. Pure; the workflow can only be one of WORKFLOWS. */
export function buildDispatch(kind, meta, env) {
  // Own properties only: WORKFLOWS["constructor"] would otherwise resolve
  // through the prototype chain to a truthy value.
  const workflow = Object.hasOwn(WORKFLOWS, kind) ? WORKFLOWS[kind] : undefined;
  if (typeof workflow !== "string") {
    throw new Error(`no workflow for invocation kind ${JSON.stringify(kind)}`);
  }
  const owner = encodeURIComponent(env.GITHUB_OWNER);
  const repo = encodeURIComponent(env.GITHUB_REPO);
  const url = `${apiBase(env)}/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;

  // Inputs must match the target workflow's declared inputs exactly, or
  // GitHub rejects the dispatch with 422.
  const inputs =
    kind === "test"
      ? {
          invocation: "test",
          cron: meta.cron,
          scheduled_time: meta.scheduledTime,
          request_id: meta.requestId,
        }
      : { catchup_days: "10" };

  return { workflow, url, body: { ref: env.GITHUB_REF || "main", inputs } };
}

// The API base is fixed to GitHub. A loopback override exists only so the
// local Wrangler test can point at a stub server; anything else is refused,
// so a config change cannot send the token to a third party.
function apiBase(env) {
  const override = env.GITHUB_API_BASE;
  if (!override) return GITHUB_API;
  if (/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(override)) return override;
  throw new Error("GITHUB_API_BASE may only point at loopback");
}

/** Remove the token from any text before it is logged or returned. */
export function redact(text, token) {
  let out = String(text ?? "");
  if (token) out = out.split(token).join("[redacted]");
  return out.slice(0, MAX_ERROR_CHARS);
}

/**
 * POST the dispatch. Returns a log-safe result object; never includes the
 * token or request headers. Retries once on a 5xx or network error.
 */
export async function dispatch(request, env, fetchImpl = fetch) {
  const token = env.GH_DISPATCH_TOKEN;
  if (!token) {
    return { ok: false, status: 0, error: "GH_DISPATCH_TOKEN is not configured" };
  }
  const init = {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": USER_AGENT,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(request.body),
  };

  let last = { ok: false, status: 0, error: "not attempted" };
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      const res = await fetchImpl(request.url, init);
      // Current API: 200 with the run id. Older behaviour: 204, no body.
      if (res.status === 200 || res.status === 204) {
        let runId = null;
        let runUrl = null;
        if (res.status === 200) {
          const data = await res.json().catch(() => ({}));
          runId = data.workflow_run_id ?? null;
          runUrl = data.html_url ?? null;
        }
        return { ok: true, status: res.status, attempt, workflow_run_id: runId, run_url: runUrl };
      }
      const text = await res.text().catch(() => "");
      let message = text;
      try {
        message = JSON.parse(text).message ?? text;
      } catch {
        // not JSON; keep the raw (truncated, redacted) text
      }
      last = { ok: false, status: res.status, attempt, error: redact(message, token) };
      if (res.status < 500) break; // 4xx will not fix itself on retry
    } catch (err) {
      last = { ok: false, status: 0, attempt, error: redact(err?.message ?? err, token) };
    }
  }
  return last;
}

function log(level, event, fields) {
  const line = JSON.stringify({ event, ...fields });
  if (level === "error") console.error(line);
  else console.log(line);
}

/** One full invocation: classify, dispatch, log. Returns the log-safe result. */
export async function run({ cron, scheduledTime, source }, env, fetchImpl = fetch) {
  const requestId = crypto.randomUUID();
  const scheduled = new Date(scheduledTime).toISOString(); // always UTC ("Z")
  const decision = source === "manual-test" ? { kind: "test" } : classify(cron, env);
  const base = { request_id: requestId, source, cron, scheduled_time_utc: scheduled, kind: decision.kind };

  log("info", "invocation", base);

  if (decision.kind !== "test" && decision.kind !== "production") {
    log("info", "skipped", { ...base, reason: decision.reason });
    return { ...base, ok: true, skipped: true, reason: decision.reason };
  }

  const request = buildDispatch(decision.kind, { cron, scheduledTime: scheduled, requestId }, env);
  const result = await dispatch(request, env, fetchImpl);
  const outcome = { ...base, workflow: request.workflow, ...result };
  log(result.ok ? "info" : "error", result.ok ? "dispatch_ok" : "dispatch_failed", outcome);
  return outcome;
}

// Constant-time comparison of two strings via their SHA-256 digests.
async function sameSecret(a, b) {
  const enc = new TextEncoder();
  const [da, db] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  const x = new Uint8Array(da);
  const y = new Uint8Array(db);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/**
 * HTTP entry point. Everything is 404 except one test endpoint, which exists
 * only while TEST_TRIGGER_KEY is set. The deploy workflow sets that key to a
 * random value, calls the endpoint once, then rotates it to another random
 * value that nobody holds. It can only ever dispatch the no-op TEST workflow.
 */
export async function handleFetch(request, env, fetchImpl = fetch) {
  const url = new URL(request.url);
  const known = url.pathname === "/__test-dispatch" || url.pathname === "/__test-auth";
  if (request.method !== "POST" || !known || !env.TEST_TRIGGER_KEY) {
    return json(404, { error: "not found" });
  }
  const auth = request.headers.get("authorization") ?? "";
  const presented = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!presented || !(await sameSecret(presented, env.TEST_TRIGGER_KEY))) {
    return json(401, { error: "unauthorized" });
  }
  // Key check only, no dispatch: lets the deploy workflow prove a rotated-out
  // key is dead without risking an extra probe run.
  if (url.pathname === "/__test-auth") return new Response(null, { status: 204 });
  const outcome = await run(
    { cron: "manual-test", scheduledTime: Date.now(), source: "manual-test" },
    env,
    fetchImpl,
  );
  return json(outcome.ok ? 200 : 502, outcome);
}

export default {
  async scheduled(controller, env) {
    const outcome = await run(
      { cron: controller.cron, scheduledTime: controller.scheduledTime, source: "cron" },
      env,
    );
    // Throwing marks the cron event as failed in Cloudflare's event history.
    if (!outcome.ok) throw new Error(`dispatch failed with status ${outcome.status}`);
  },
  fetch(request, env) {
    return handleFetch(request, env);
  },
};
