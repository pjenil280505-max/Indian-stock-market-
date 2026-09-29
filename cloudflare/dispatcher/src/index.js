// Cloudflare Cron -> GitHub Actions workflow_dispatch.
//
// This Worker is a trigger. It holds no market data, never talks to Neon or
// Upstox, and runs no pipeline code. The Python pipeline runs on GitHub's
// runners under the existing `database-writer` concurrency lock.
//
// Two kinds of invocation, kept strictly apart:
//   test        -> cloudflare-dispatch-probe.yml, a no-op workflow with no
//                  secrets and no database access. No readiness check.
//   production  -> daily-data-update.yml, gated by src/readiness.js: it is
//                  dispatched only once NSE has published the day's file and
//                  no run for that date is already queued, running or done.
//                  DISABLED: requires PRODUCTION_ENABLED = "true" AND a cron
//                  listed in PRODUCTION_CRONS; neither is set.
//
// Outbound requests: api.github.com (dispatch, and reading today's runs for
// the gate) and GETs of the allowlisted NSE files (readiness and the
// key-guarded probe endpoints). Nothing else.
//
// All times are UTC. Cloudflare evaluates cron expressions in UTC and
// `scheduledTime` is a UTC epoch; the trade date is derived in IST.

import { probe, resolveTarget } from "./nse_probe.js";
import { ALERT_KINDS, checkStaleness } from "./monitor.js";
import { evaluate, runTag } from "./readiness.js";

export const WORKFLOWS = Object.freeze({
  test: "cloudflare-dispatch-probe.yml",
  production: "daily-data-update.yml",
  alert: "pipeline-alert.yml", // Phase 1.1: opens/updates a GitHub Issue
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
  if (list(env.STALENESS_CRONS).includes(cron)) {
    // The staleness check can dispatch the real pipeline, so it is gated by
    // the same production switch.
    if (env.PRODUCTION_ENABLED === "true") return { kind: "staleness" };
    return { kind: "blocked", reason: "production dispatch is disabled" };
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
  let inputs;
  if (kind === "test") {
    inputs = {
      invocation: "test",
      cron: meta.cron,
      scheduled_time: meta.scheduledTime,
      request_id: meta.requestId,
    };
  } else if (kind === "alert") {
    if (!ALERT_KINDS.includes(meta.alertKind)) throw new Error(`unknown alert kind ${JSON.stringify(meta.alertKind)}`);
    inputs = {
      kind: meta.alertKind,
      trade_date: meta.tradeDate,
      detail: cleanDetail(meta.detail),
      request_id: meta.requestId,
    };
  } else {
    inputs = {
      catchup_days: "10",
      request_id: meta.requestId,
      trade_date: meta.tradeDate,
    };
  }

  return { workflow, url, body: { ref: env.GITHUB_REF || "main", inputs } };
}

/** Alert detail is shown in a GitHub Issue: one line, bounded, no mentions
 *  or markdown code fences. Pure. */
export function cleanDetail(text) {
  return String(text ?? "")
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/[@`]/g, "")
    .trim()
    .slice(0, 300);
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

  if (decision.kind === "staleness") return runStaleness({ base, scheduledTime, requestId, env, fetchImpl });

  if (decision.kind !== "test" && decision.kind !== "production") {
    log("info", "skipped", { ...base, reason: decision.reason });
    return { ...base, ok: true, skipped: true, reason: decision.reason };
  }

  let readiness = null;
  if (decision.kind === "production") {
    // Gate: dispatch the real pipeline only once NSE has published and no
    // run for this date is already queued, running or done.
    readiness = await evaluate({ scheduledTime, env, fetchImpl });
    base.trade_date = readiness.trade_date;
    if (readiness.decision.action !== "dispatch") {
      log("info", "not_dispatched", { ...base, reason: readiness.decision.reason, readiness });
      return { ...base, ok: true, skipped: true, reason: readiness.decision.reason, readiness };
    }
  }

  const request = buildDispatch(
    decision.kind,
    { cron, scheduledTime: scheduled, requestId, tradeDate: readiness?.trade_date },
    env,
  );
  const result = await dispatch(request, env, fetchImpl);
  const outcome = { ...base, workflow: request.workflow, ...result, ...(readiness ? { readiness } : {}) };
  log(result.ok ? "info" : "error", result.ok ? "dispatch_ok" : "dispatch_failed", outcome);

  // Phase 1.1 alerts for the production path. Best effort: an alert that
  // cannot be sent is logged, never allowed to change the dispatch outcome.
  if (decision.kind === "production") {
    const date = readiness.trade_date;
    if (!result.ok) {
      outcome.alerts = [await sendAlert("dispatch_failed", date, `dispatch HTTP ${result.status}: ${result.error ?? ""}`, env, fetchImpl)];
    } else if (readiness.decision.completeness === "unverified") {
      outcome.alerts = [await sendAlert("nse_blocked", date, readiness.decision.reason, env, fetchImpl)];
    }
  }
  return outcome;
}

/** Dispatch the alert workflow. Never throws; returns a log-safe result. */
export async function sendAlert(alertKind, tradeDate, detail, env, fetchImpl = fetch) {
  try {
    const request = buildDispatch("alert", { alertKind, tradeDate, detail, requestId: crypto.randomUUID() }, env);
    const result = await dispatch(request, env, fetchImpl);
    log(result.ok ? "info" : "error", result.ok ? "alert_sent" : "alert_failed", { alert: alertKind, trade_date: tradeDate, status: result.status });
    return { alert: alertKind, ok: result.ok, status: result.status };
  } catch (err) {
    log("error", "alert_failed", { alert: alertKind, trade_date: tradeDate, error: String(err?.message ?? err).slice(0, 120) });
    return { alert: alertKind, ok: false, status: 0 };
  }
}

/** Phase 1.1 daily staleness check: at most ONE emergency dispatch, plus alerts. */
async function runStaleness({ base, scheduledTime, requestId, env, fetchImpl }) {
  const check = await checkStaleness({ scheduledTime, env, fetchImpl });
  const date = check.trade_date;
  const outcome = { ...base, trade_date: date, staleness: check, alerts: [], ok: true };

  if (check.decision.action === "emergency_dispatch") {
    const request = buildDispatch("production", { requestId, tradeDate: date }, env);
    const result = await dispatch(request, env, fetchImpl);
    outcome.emergency_dispatch = { workflow: request.workflow, ...result };
    outcome.ok = result.ok;
    const detail = `${check.decision.reason}; emergency dispatch ${result.ok ? "sent" : `FAILED (HTTP ${result.status})`}`;
    outcome.alerts.push(await sendAlert(check.decision.alert, date, detail, env, fetchImpl));
  } else if (check.decision.action === "alert") {
    outcome.alerts.push(await sendAlert(check.decision.alert, date, check.decision.reason, env, fetchImpl));
  }
  if (check.token.alert) {
    const detail = `dispatch token expires ${check.token.expires} (${check.token.days} day(s)); rotate GH_DISPATCH_TOKEN`;
    outcome.alerts.push(await sendAlert("token_expiring", date, detail, env, fetchImpl));
  }
  log(outcome.ok ? "info" : "error", "staleness_check", {
    ...base, trade_date: date, decision: check.decision, token_days: check.token.days ?? null,
    alerts: outcome.alerts.map((a) => `${a.alert}:${a.ok ? "sent" : "failed"}`),
  });
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
  const known = ["/__test-dispatch", "/__test-auth", "/__test-nse", "/__test-readiness", "/__test-staleness"].includes(url.pathname);
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
  if (url.pathname === "/__test-nse") return nseProbe(request, url, fetchImpl);
  if (url.pathname === "/__test-readiness") return readinessDryRun(url, env, fetchImpl);
  if (url.pathname === "/__test-staleness") return stalenessDryRun(url, env, fetchImpl);
  const outcome = await run(
    { cron: "manual-test", scheduledTime: Date.now(), source: "manual-test" },
    env,
    fetchImpl,
  );
  return json(outcome.ok ? 200 : 502, outcome);
}

// Read-only Cloudflare -> NSE connectivity probe. One allowlisted NSE
// resource per call; measurements only, nothing stored. Reachable ONLY from
// the key-guarded endpoint above - the cron handler never calls it.
async function nseProbe(request, url, fetchImpl) {
  let target;
  try {
    target = resolveTarget(url.searchParams.get("target"), url.searchParams.get("date"));
  } catch (err) {
    return json(400, { error: err.message });
  }
  const result = await probe(target, { fetchImpl });
  const edge = { colo: request.cf?.colo ?? null, country: request.cf?.country ?? null };
  log("info", "nse_probe", { target: result.target, status: result.status, total_ms: result.total_ms, colo: edge.colo });
  return json(200, { ...result, cloudflare: edge, probed_at_utc: new Date().toISOString() });
}

// DRY RUN of the production gate for a given trade date and UTC time of
// day. Reads GitHub and NSE exactly as a production fire would, returns the
// decision, and NEVER dispatches - whatever PRODUCTION_ENABLED says.
async function readinessDryRun(url, env, fetchImpl) {
  const date = url.searchParams.get("date") ?? "";
  const at = url.searchParams.get("at") ?? "11:45";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^\d{2}:\d{2}$/.test(at)) {
    return json(400, { error: "date=YYYY-MM-DD and at=HH:MM (UTC) required" });
  }
  const scheduledTime = Date.parse(`${date}T${at}:00Z`);
  if (Number.isNaN(scheduledTime)) return json(400, { error: "invalid date/time" });
  const result = await evaluate({ scheduledTime, env, fetchImpl });
  log("info", "readiness_dry_run", { trade_date: result.trade_date, at_utc: at, decision: result.decision });
  return json(200, { dry_run: true, dispatched: false, at_utc: at, run_tag: runTag(result.trade_date), ...result });
}

// DRY RUN of the Phase 1.1 staleness check for a date and UTC time: reads
// GitHub and NSE, returns the decision and token status, NEVER dispatches
// anything (no emergency dispatch, no alert).
async function stalenessDryRun(url, env, fetchImpl) {
  const date = url.searchParams.get("date") ?? "";
  const at = url.searchParams.get("at") ?? "15:05";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^\d{2}:\d{2}$/.test(at)) {
    return json(400, { error: "date=YYYY-MM-DD and at=HH:MM (UTC) required" });
  }
  const scheduledTime = Date.parse(`${date}T${at}:00Z`);
  if (Number.isNaN(scheduledTime)) return json(400, { error: "invalid date/time" });
  const check = await checkStaleness({ scheduledTime, env, fetchImpl });
  log("info", "staleness_dry_run", { trade_date: check.trade_date, decision: check.decision });
  return json(200, { dry_run: true, dispatched: false, alerted: false, at_utc: at, ...check });
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
