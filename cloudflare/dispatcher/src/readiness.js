// Publication readiness gate for PRODUCTION dispatches.
//
// The daily pipeline must not be dispatched until NSE has actually published
// the day's file. A production cron fires repeatedly across a window; each
// fire asks three questions, cheapest first, and dispatches only if all pass:
//
//   1. Has a Cloudflare-tagged run for this trade date already been
//      dispatched (queued, running or succeeded)?  -> skip
//      Have too many of them failed?                -> skip (retry budget)
//   2. Is the delivery bhavcopy published AND for the right date AND
//      plausibly complete?                          -> dispatch ("complete")
//   3. Only in the final window of the day: is the UDiFF bhavcopy published
//      for the right date?                          -> dispatch ("partial";
//      the pipeline marks the day partial and enriches it on a later run)
//   Otherwise                                       -> skip (not published)
//
// All reads. Nothing here writes anywhere or dispatches anything; the
// caller dispatches only on a "dispatch" decision.

import { USER_AGENT, inspectZip, resolveTarget } from "./nse_probe.js";

export const MIN_DELIVERY_ROWS = 500; // full market is ~3,400; guards truncation
export const MAX_FAILED_RUNS = 2;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const IST_OFFSET_MS = 330 * 60 * 1000;
const ACTIVE = new Set(["queued", "in_progress", "waiting", "requested", "pending"]);

/** The Indian trading date for a UTC instant. All inputs are UTC. */
export function istDate(epochMs) {
  return new Date(epochMs + IST_OFFSET_MS).toISOString().slice(0, 10);
}

/** "2026-09-24" -> "24-Sep-2026", the DATE1 format in sec_bhavdata_full. */
export function nseDate(isoDate) {
  const [y, m, d] = isoDate.split("-");
  return `${d}-${MONTHS[Number(m) - 1]}-${y}`;
}

/** True when this fire is at or after the day's final-window time (UTC "HH:MM"). */
export function isFinalWindow(epochMs, finalUtc) {
  const [h, m] = String(finalUtc || "23:59").split(":").map(Number);
  const t = new Date(epochMs);
  return t.getUTCHours() * 60 + t.getUTCMinutes() >= h * 60 + m;
}

/** The run-name tag the daily workflow shows for a Cloudflare dispatch. */
export const runTag = (tradeDate) => `cloudflare ${tradeDate}`;

/** Summarise today's Cloudflare-tagged runs from a GitHub runs listing. Pure. */
export function summariseRuns(runs, tradeDate) {
  const tagged = (runs ?? []).filter((r) => String(r.display_title ?? r.name ?? "").includes(runTag(tradeDate)));
  return {
    total: tagged.length,
    active: tagged.filter((r) => ACTIVE.has(r.status)).length,
    succeeded: tagged.filter((r) => r.status === "completed" && r.conclusion === "success").length,
    failed: tagged.filter((r) => r.status === "completed" && r.conclusion !== "success").length,
  };
}

/**
 * The gate. Pure: takes observations, returns { action, reason, ... }.
 * github: summariseRuns() output, or { error } if GitHub could not be read.
 * delivery / udiff: fileCheck() results, or null if not checked.
 */
export function decide({ github, delivery, udiff, finalWindow }) {
  if (!github || github.error) {
    // Fail closed: without knowing what already ran, a dispatch could duplicate.
    return { action: "skip", reason: "github state unknown", error: github?.error ?? "missing" };
  }
  if (github.active > 0) return { action: "skip", reason: "a run for this date is already queued or running" };
  if (github.succeeded > 0) return { action: "skip", reason: "already dispatched and succeeded for this date" };
  if (github.failed >= MAX_FAILED_RUNS) {
    return { action: "skip", reason: `retry budget exhausted (${github.failed} failed runs)` };
  }
  if (delivery?.ready) return { action: "dispatch", completeness: "complete", reason: "delivery bhavcopy published" };
  if (finalWindow && udiff?.ready) {
    return { action: "dispatch", completeness: "partial", reason: "final window: UDiFF published, delivery not yet" };
  }
  return { action: "skip", reason: finalWindow ? "not published by the final window" : "not published yet" };
}

// ---- observations (reads only) ------------------------------------------

async function timedGet(url, fetchImpl, headers) {
  const t0 = Date.now();
  const res = await fetchImpl(url, { method: "GET", headers, redirect: "manual" });
  const body = await res.arrayBuffer();
  return { status: res.status, body, ms: Date.now() - t0, contentType: res.headers.get("content-type") ?? "" };
}

/** Is the delivery bhavcopy for tradeDate published, dated right, and complete? */
export async function checkDelivery(tradeDate, fetchImpl = fetch) {
  const { url } = resolveTarget("delivery_bhavcopy", tradeDate);
  try {
    const r = await timedGet(url, fetchImpl, { "User-Agent": USER_AGENT });
    const base = { file: "delivery_bhavcopy", status: r.status, ms: r.ms, bytes: r.body.byteLength };
    if (r.status !== 200) return { ...base, ready: false, why: `HTTP ${r.status}` };
    return { ...base, ...validateDelivery(new TextDecoder().decode(r.body), tradeDate) };
  } catch (err) {
    return { file: "delivery_bhavcopy", status: 0, ready: false, why: String(err?.message ?? err).slice(0, 120) };
  }
}

/** Pure validation of a delivery bhavcopy's text for tradeDate. */
export function validateDelivery(text, tradeDate) {
  const lines = text.split("\n").filter((l) => l.trim());
  if (lines.length < 2) return { ready: false, why: "empty file" };
  const header = lines[0].split(",").map((c) => c.trim());
  const dateCol = header.indexOf("DATE1");
  if (dateCol === -1 || !header.includes("DELIV_PER")) return { ready: false, why: "unexpected header" };
  const firstDate = (lines[1].split(",")[dateCol] ?? "").trim();
  if (firstDate !== nseDate(tradeDate)) return { ready: false, why: `file dated ${firstDate || "?"}`, rows: lines.length - 1 };
  const rows = lines.length - 1;
  if (rows < MIN_DELIVERY_ROWS) return { ready: false, why: `only ${rows} rows`, rows };
  return { ready: true, rows, first_date: firstDate };
}

/** Is the UDiFF bhavcopy for tradeDate published and dated right? */
export async function checkUdiff(tradeDate, fetchImpl = fetch) {
  const { url } = resolveTarget("udiff_bhavcopy", tradeDate);
  try {
    const r = await timedGet(url, fetchImpl, { "User-Agent": USER_AGENT });
    const base = { file: "udiff_bhavcopy", status: r.status, ms: r.ms, bytes: r.body.byteLength };
    if (r.status !== 200) return { ...base, ready: false, why: `HTTP ${r.status}` };
    const zip = await inspectZip(r.body, { lines: 2 });
    if (!zip.valid || !zip.csv_header?.startsWith("TradDt")) return { ...base, ready: false, why: "unexpected archive" };
    const first = (zip.first_row ?? "").split(",")[0];
    if (first !== tradeDate) return { ...base, ready: false, why: `file dated ${first || "?"}` };
    return { ...base, ready: true, first_date: first };
  } catch (err) {
    return { file: "udiff_bhavcopy", status: 0, ready: false, why: String(err?.message ?? err).slice(0, 120) };
  }
}

/** Today's daily-workflow runs, read from GitHub. Read-only. */
export async function readRuns(tradeDate, env, fetchImpl = fetch) {
  const url =
    `https://api.github.com/repos/${encodeURIComponent(env.GITHUB_OWNER)}/${encodeURIComponent(env.GITHUB_REPO)}` +
    `/actions/workflows/daily-data-update.yml/runs?event=workflow_dispatch&created=%3E%3D${tradeDate}&per_page=50`;
  try {
    const res = await fetchImpl(url, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nse-pipeline-dispatcher",
      },
    });
    if (res.status !== 200) return { error: `GitHub HTTP ${res.status}` };
    const data = await res.json();
    return summariseRuns(data.workflow_runs, tradeDate);
  } catch (err) {
    return { error: "GitHub request failed" };
  }
}

/**
 * Gather observations and decide. Checks cheapest-first and stops early:
 * NSE is not contacted when GitHub already shows today's run.
 */
export async function evaluate({ scheduledTime, tradeDate, env, fetchImpl = fetch }) {
  const date = tradeDate ?? istDate(scheduledTime);
  const finalWindow = isFinalWindow(scheduledTime, env.READINESS_FINAL_UTC);
  const github = await readRuns(date, env, fetchImpl);
  let delivery = null;
  let udiff = null;
  const early = decide({ github, delivery: null, udiff: null, finalWindow: false });
  if (early.action === "skip" && early.reason !== "not published yet") {
    return { trade_date: date, final_window: finalWindow, github, delivery, udiff, decision: early };
  }
  delivery = await checkDelivery(date, fetchImpl);
  if (!delivery.ready && finalWindow) udiff = await checkUdiff(date, fetchImpl);
  const decision = decide({ github, delivery, udiff, finalWindow });
  return { trade_date: date, final_window: finalWindow, github, delivery, udiff, decision };
}
