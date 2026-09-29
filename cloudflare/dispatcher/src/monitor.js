// Phase 1.1 monitoring: the daily staleness check and token-expiry warning.
//
// Runs once per weekday on its own cron (STALENESS_CRONS), after the
// production window has closed. It answers one question - did today's data
// load? - and acts only when the answer is no:
//
//   today's Cloudflare run succeeded            -> ok (nothing happens)
//   a run is still queued/running               -> wait (nothing happens)
//   GitHub unreadable                           -> alert github_unreadable
//   NSE published, but no successful run        -> ONE emergency dispatch
//                                                  + alert stale_dispatched
//   NSE unreachable from Cloudflare, no run     -> ONE emergency dispatch
//                                                  + alert nse_blocked
//   nothing published (404)                     -> ok_holiday (no alert:
//                                                  holidays are normal)
//
// Emergency dispatches carry the normal "cloudflare <date>" tag, so the
// duplicate check and the GitHub fallback see them like any other run.
// Separately, it warns before the dispatch token expires.
//
// Everything here is a read or a pure decision; the Worker performs any
// dispatch the decision asks for.

import { checkDelivery, checkUdiff, istDate, readRuns } from "./readiness.js";

export const ALERT_KINDS = Object.freeze([
  "nse_blocked",
  "stale_dispatched",
  "dispatch_failed",
  "token_expiring",
  "github_unreadable",
  "test_alert",
]);

const DAY_MS = 24 * 60 * 60 * 1000;

/** Pure staleness decision from observations. */
export function staleDecision({ github, delivery, udiff }) {
  if (!github || github.error) {
    return { action: "alert", alert: "github_unreadable", reason: `GitHub runs unreadable (${github?.error ?? "missing"})` };
  }
  if (github.succeeded > 0) return { action: "ok", reason: "today's run succeeded" };
  if (github.active > 0) return { action: "wait", reason: "today's run is queued or running" };
  if (delivery?.ready || udiff?.ready) {
    return {
      action: "emergency_dispatch",
      alert: "stale_dispatched",
      reason: `NSE published but no successful run (${github.failed} failed)`,
    };
  }
  if (delivery?.blocked) {
    return {
      action: "emergency_dispatch",
      alert: "nse_blocked",
      reason: "NSE unreachable from Cloudflare and no successful run today",
    };
  }
  return { action: "ok_holiday", reason: "nothing published today (holiday or very late publication)" };
}

/** Parse GitHub's "2026-12-24 12:00:00 UTC" expiry header. Null if absent/unparseable. */
export function parseExpiry(header) {
  if (!header) return null;
  const iso = String(header).trim().replace(" ", "T").replace(/\s*UTC$/, "Z").replace(/\s*([+-]\d{2})(\d{2})$/, "$1:$2");
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms;
}

/** Days until expiry and whether today is a warning day. Pure. */
export function tokenWarning(header, nowMs, warnDays = "14,7,3,1") {
  const expires = parseExpiry(header);
  if (expires === null) return { known: false, alert: false };
  const days = Math.floor((expires - nowMs) / DAY_MS);
  const thresholds = String(warnDays).split(",").map((d) => Number(d.trim())).filter(Number.isFinite);
  const alert = days <= 0 || thresholds.includes(days);
  return { known: true, days, expires: new Date(expires).toISOString(), alert };
}

/** Gather observations (reads only) and decide. */
export async function checkStaleness({ scheduledTime, env, fetchImpl = fetch }) {
  const date = istDate(scheduledTime);
  const github = await readRuns(date, env, fetchImpl);
  let delivery = null;
  let udiff = null;
  if (github && !github.error && !github.succeeded && !github.active) {
    delivery = await checkDelivery(date, fetchImpl);
    if (!delivery.ready && !delivery.blocked) udiff = await checkUdiff(date, fetchImpl);
  }
  const decision = staleDecision({ github, delivery, udiff });
  const token = tokenWarning(github?.token_expires, scheduledTime, env.TOKEN_WARN_DAYS);
  return { trade_date: date, github, delivery, udiff, decision, token };
}
