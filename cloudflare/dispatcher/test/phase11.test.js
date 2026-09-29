// Phase 1.1 reliability hardening: blocked-vs-unpublished, staleness check,
// alerts, token expiry.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import worker, { buildDispatch, classify, cleanDetail, handleFetch, run } from "../src/index.js";
import { ALERT_KINDS, parseExpiry, staleDecision, tokenWarning } from "../src/monitor.js";
import { checkDelivery, decide, evaluate, isBlocked, nseDate, readRuns, runTag } from "../src/readiness.js";
import { deliveryCsv, makeFullZip, quiet, router } from "./helpers.js";

const TOKEN = "github_pat_TESTONLY_phase11_0123456789";
const KEY = "k".repeat(64);
const PROD = "*/15 11-14 * * 1-5";
const STALE = "5 15 * * 1-5";
const ENV = Object.freeze({
  GITHUB_OWNER: "pjenil280505-max", GITHUB_REPO: "Indian-stock-market-", GITHUB_REF: "main",
  TEST_CRONS: "37 4 * * *", PRODUCTION_CRONS: PROD, PRODUCTION_ENABLED: "true",
  READINESS_FINAL_UTC: "14:45", BLOCKED_DISPATCH_AFTER_UTC: "12:00",
  STALENESS_CRONS: STALE, TOKEN_WARN_DAYS: "14,7,3,1",
  GH_DISPATCH_TOKEN: TOKEN, TEST_TRIGGER_KEY: KEY,
});
const D = "2026-09-29";
const AT = (hhmm) => Date.parse(`${D}T${hhmm}:00Z`);
const wf = (name) => readFileSync(new URL(`../../../.github/workflows/${name}`, import.meta.url), "utf8");

const runs = (list, headers = {}) => () =>
  new Response(JSON.stringify({ workflow_runs: list }), { status: 200, headers });
const noRuns = runs([]);
const tagged = (status, conclusion) => ({ display_title: `Daily data update (${runTag(D)} x)`, status, conclusion });
const csvOk = () => new Response(deliveryCsv(nseDate(D), 3400), { status: 200, headers: { "content-type": "text/csv" } });
const notFound = () => new Response("<html>404</html>", { status: 404, headers: { "content-type": "text/html" } });
const forbidden = () => new Response("<html>Access Denied</html>", { status: 403, headers: { "content-type": "text/html" } });
const accepted = () => new Response(null, { status: 204 });
const posts = (calls) => calls.filter((c) => c.url.endsWith("/dispatches"));
const postedTo = (calls) => posts(calls).map((c) => c.url.match(/workflows\/([^/]+)\/dispatches/)[1]);
const bodyOf = (call) => JSON.parse(call.init.body);

// ---- A. blocked vs not published ----------------------------------------

test("isBlocked: 404 is 'not published'; 403/429/5xx/0 and HTML-200 are 'unreachable'", () => {
  assert.equal(isBlocked(404, "text/html"), false);
  assert.equal(isBlocked(200, "text/csv"), false);
  for (const s of [403, 429, 500, 502, 503, 0]) assert.equal(isBlocked(s, ""), true, String(s));
  assert.equal(isBlocked(200, "text/html; charset=utf-8"), true);
});

test("checkDelivery flags 403 and network failure as blocked, 404 as not", async () => {
  assert.equal((await checkDelivery(D, router([["sec_bhavdata", forbidden]]).impl)).blocked, true);
  assert.equal((await checkDelivery(D, async () => { throw new Error("ECONNRESET"); })).blocked, true);
  assert.ok(!(await checkDelivery(D, router([["sec_bhavdata", notFound]]).impl)).blocked);
});

const none = { total: 0, active: 0, succeeded: 0, failed: 0 };

test("decide: blocked waits before the blocked-dispatch time, dispatches 'unverified' after", () => {
  const blocked = { ready: false, blocked: true };
  assert.equal(decide({ github: none, delivery: blocked, finalWindow: false, blockedDispatchOk: false }).action, "skip");
  const d = decide({ github: none, delivery: blocked, finalWindow: false, blockedDispatchOk: true });
  assert.deepEqual([d.action, d.completeness], ["dispatch", "unverified"]);
});

test("decide: duplicate protection still wins over the blocked branch", () => {
  const blocked = { ready: false, blocked: true };
  for (const github of [{ ...none, succeeded: 1 }, { ...none, active: 1 }]) {
    assert.equal(decide({ github, delivery: blocked, blockedDispatchOk: true }).action, "skip");
  }
});

test("without BLOCKED_DISPATCH_AFTER_UTC the gate behaves exactly as Phase 1 (never dispatches when blocked)", async () => {
  const phase1 = { ...ENV };
  delete phase1.BLOCKED_DISPATCH_AFTER_UTC;
  const { impl } = router([["/runs?", noRuns], ["sec_bhavdata", forbidden], ["BhavCopy", forbidden]]);
  for (const at of ["12:00", "14:45"]) {
    const r = await evaluate({ scheduledTime: AT(at), env: phase1, fetchImpl: impl });
    assert.equal(r.decision.action, "skip", at);
  }
});

test("production fire when NSE blocks Cloudflare: dispatch daily + nse_blocked alert", async () => {
  const { impl, calls } = router([["/dispatches", accepted], ["/runs?", noRuns], ["sec_bhavdata", forbidden]]);
  const { value } = await quiet(() => run({ cron: PROD, scheduledTime: AT("12:00"), source: "cron" }, ENV, impl));
  assert.equal(value.ok, true);
  assert.deepEqual(postedTo(calls), ["daily-data-update.yml", "pipeline-alert.yml"]);
  assert.deepEqual(bodyOf(posts(calls)[1]).inputs.kind, "nse_blocked");
  assert.equal(bodyOf(posts(calls)[1]).inputs.trade_date, D);
});

test("production fire before 12:00 when blocked: nothing dispatched, no alert", async () => {
  const { impl, calls } = router([["/runs?", noRuns], ["sec_bhavdata", forbidden]]);
  await quiet(() => run({ cron: PROD, scheduledTime: AT("11:45"), source: "cron" }, ENV, impl));
  assert.equal(posts(calls).length, 0);
});

test("normal publication still dispatches once with no alert (Phase 1 path unchanged)", async () => {
  const { impl, calls } = router([["/dispatches", accepted], ["/runs?", noRuns], ["sec_bhavdata", csvOk]]);
  await quiet(() => run({ cron: PROD, scheduledTime: AT("11:30"), source: "cron" }, ENV, impl));
  assert.deepEqual(postedTo(calls), ["daily-data-update.yml"]);
});

test("failed production dispatch raises a dispatch_failed alert (best effort)", async () => {
  const { impl, calls } = router([
    ["daily-data-update.yml/dispatches", () => new Response('{"message":"Bad credentials"}', { status: 401 })],
    ["pipeline-alert.yml/dispatches", accepted],
    ["/runs?", noRuns], ["sec_bhavdata", csvOk],
  ]);
  const { value } = await quiet(() => run({ cron: PROD, scheduledTime: AT("11:30"), source: "cron" }, ENV, impl));
  assert.equal(value.ok, false);
  assert.equal(bodyOf(posts(calls)[1]).inputs.kind, "dispatch_failed");
});

// ---- B. staleness check --------------------------------------------------

test("staleDecision table", () => {
  assert.equal(staleDecision({ github: { error: "GitHub HTTP 401" } }).alert, "github_unreadable");
  assert.equal(staleDecision({ github: { ...none, succeeded: 1 } }).action, "ok");
  assert.equal(staleDecision({ github: { ...none, active: 1 } }).action, "wait");
  assert.deepEqual(
    [staleDecision({ github: none, delivery: { ready: true } }).action, staleDecision({ github: none, delivery: { ready: true } }).alert],
    ["emergency_dispatch", "stale_dispatched"]);
  assert.equal(staleDecision({ github: none, delivery: { ready: false }, udiff: { ready: true } }).alert, "stale_dispatched");
  assert.equal(staleDecision({ github: none, delivery: { ready: false, blocked: true } }).alert, "nse_blocked");
  assert.equal(staleDecision({ github: none, delivery: { ready: false }, udiff: { ready: false } }).action, "ok_holiday");
});

test("staleness: today loaded -> no NSE contact, no dispatch, no alert", async () => {
  const { impl, calls } = router([["/runs?", runs([tagged("completed", "success")])]]);
  const { value } = await quiet(() => run({ cron: STALE, scheduledTime: AT("15:05"), source: "cron" }, ENV, impl));
  assert.equal(value.ok, true);
  assert.equal(calls.length, 1);
  assert.equal(posts(calls).length, 0);
});

test("staleness: published but not loaded -> ONE emergency dispatch (tagged) + stale_dispatched alert", async () => {
  const { impl, calls } = router([["/dispatches", accepted], ["/runs?", runs([tagged("completed", "failure")])], ["sec_bhavdata", csvOk]]);
  const { value } = await quiet(() => run({ cron: STALE, scheduledTime: AT("15:05"), source: "cron" }, ENV, impl));
  assert.deepEqual(postedTo(calls), ["daily-data-update.yml", "pipeline-alert.yml"]);
  const daily = bodyOf(posts(calls)[0]).inputs;
  assert.equal(daily.trade_date, D);
  assert.equal(daily.request_id, value.request_id);
  assert.equal(bodyOf(posts(calls)[1]).inputs.kind, "stale_dispatched");
});

test("staleness: NSE blocked from Cloudflare -> emergency dispatch + nse_blocked alert", async () => {
  const { impl, calls } = router([["/dispatches", accepted], ["/runs?", noRuns], ["sec_bhavdata", forbidden]]);
  await quiet(() => run({ cron: STALE, scheduledTime: AT("15:05"), source: "cron" }, ENV, impl));
  assert.deepEqual(postedTo(calls), ["daily-data-update.yml", "pipeline-alert.yml"]);
  assert.equal(bodyOf(posts(calls)[1]).inputs.kind, "nse_blocked");
});

test("staleness: holiday (both files 404) -> nothing dispatched, no alert", async () => {
  const { impl, calls } = router([["/runs?", noRuns], ["sec_bhavdata", notFound], ["BhavCopy", notFound]]);
  await quiet(() => run({ cron: STALE, scheduledTime: AT("15:05"), source: "cron" }, ENV, impl));
  assert.equal(posts(calls).length, 0);
});

test("staleness: GitHub unreadable -> alert only, never a blind dispatch", async () => {
  const { impl, calls } = router([["/runs?", () => new Response("", { status: 401 })], ["pipeline-alert.yml", accepted]]);
  await quiet(() => run({ cron: STALE, scheduledTime: AT("15:05"), source: "cron" }, ENV, impl));
  assert.deepEqual(postedTo(calls), ["pipeline-alert.yml"]);
  assert.equal(bodyOf(posts(calls)[0]).inputs.kind, "github_unreadable");
});

test("staleness: a run still in progress -> wait, nothing dispatched", async () => {
  const { impl, calls } = router([["/runs?", runs([tagged("in_progress")])]]);
  await quiet(() => run({ cron: STALE, scheduledTime: AT("15:05"), source: "cron" }, ENV, impl));
  assert.equal(posts(calls).length, 0);
});

test("staleness is gated by PRODUCTION_ENABLED like the production cron", async () => {
  const off = { ...ENV, PRODUCTION_ENABLED: "false" };
  assert.equal(classify(STALE, off).kind, "blocked");
  assert.equal(classify(STALE, ENV).kind, "staleness");
  const { impl, calls } = router([]);
  await quiet(() => run({ cron: STALE, scheduledTime: AT("15:05"), source: "cron" }, off, impl));
  assert.equal(calls.length, 0);
});

test("scheduled handler throws when the emergency dispatch fails (visible in Cloudflare events)", async () => {
  const orig = globalThis.fetch;
  globalThis.fetch = router([
    ["daily-data-update.yml/dispatches", () => new Response("", { status: 500 })],
    ["pipeline-alert.yml/dispatches", accepted],
    ["/runs?", noRuns], ["sec_bhavdata", csvOk],
  ]).impl;
  try {
    await quiet(() => assert.rejects(worker.scheduled({ cron: STALE, scheduledTime: AT("15:05") }, ENV)));
  } finally {
    globalThis.fetch = orig;
  }
});

// ---- D. token expiry ------------------------------------------------------

test("parseExpiry reads GitHub's header format; absent or junk -> null", () => {
  assert.equal(parseExpiry("2026-10-13 00:00:00 UTC"), Date.parse("2026-10-13T00:00:00Z"));
  assert.equal(parseExpiry("2026-10-13 00:00:00 +0530"), Date.parse("2026-10-13T00:00:00+05:30"));
  assert.equal(parseExpiry(null), null);
  assert.equal(parseExpiry("soon"), null);
});

test("tokenWarning alerts only on the configured days, and when expired", () => {
  const now = Date.parse("2026-09-29T15:05:00Z");
  const inDays = (n) => new Date(now + n * 86400000 + 3600000).toISOString().replace("T", " ").slice(0, 19) + " UTC";
  assert.equal(tokenWarning(inDays(14), now).alert, true);
  assert.equal(tokenWarning(inDays(13), now).alert, false);
  assert.equal(tokenWarning(inDays(7), now).alert, true);
  assert.equal(tokenWarning(inDays(0), now).alert, true);
  assert.equal(tokenWarning(inDays(-2), now).alert, true);
  assert.deepEqual(tokenWarning(undefined, now), { known: false, alert: false });
});

test("readRuns picks up the token-expiry header when present, adds nothing when absent", async () => {
  const withHeader = router([["/runs?", runs([], { "github-authentication-token-expiration": "2026-10-13 00:00:00 UTC" })]]);
  assert.equal((await readRuns(D, ENV, withHeader.impl)).token_expires, "2026-10-13 00:00:00 UTC");
  assert.ok(!("token_expires" in (await readRuns(D, ENV, router([["/runs?", noRuns]]).impl))));
});

test("staleness raises token_expiring 7 days out, alongside an ok day", async () => {
  const exp = new Date(AT("15:05") + 7 * 86400000 + 3600000).toISOString().replace("T", " ").slice(0, 19) + " UTC";
  const { impl, calls } = router([
    ["/runs?", runs([tagged("completed", "success")], { "github-authentication-token-expiration": exp })],
    ["pipeline-alert.yml", accepted],
  ]);
  await quiet(() => run({ cron: STALE, scheduledTime: AT("15:05"), source: "cron" }, ENV, impl));
  assert.deepEqual(postedTo(calls), ["pipeline-alert.yml"]);
  assert.equal(bodyOf(posts(calls)[0]).inputs.kind, "token_expiring");
});

// ---- C. alert plumbing ----------------------------------------------------

test("alert kinds, workflow choices and the alert script agree", () => {
  const yml = wf("pipeline-alert.yml");
  const options = yml.match(/options: \[([^\]]+)\]/)[1].split(",").map((s) => s.trim());
  assert.deepEqual([...ALERT_KINDS].sort(), [...options].sort());
  const script = readFileSync(new URL("../../../scripts/raise_alert.sh", import.meta.url), "utf8");
  const scriptKinds = script.match(/^KINDS="([^"]+)"/m)[1].split(" ");
  for (const k of ALERT_KINDS) assert.ok(scriptKinds.includes(k), k);
});

test("alert dispatch inputs match pipeline-alert.yml's declared inputs exactly", () => {
  const yml = wf("pipeline-alert.yml");
  const req = buildDispatch("alert", { alertKind: "test_alert", tradeDate: D, detail: "x", requestId: "r" }, ENV);
  assert.match(req.url, /pipeline-alert\.yml\/dispatches$/);
  for (const key of Object.keys(req.body.inputs)) assert.match(yml, new RegExp(`^      ${key}:$`, "m"));
  assert.throws(() => buildDispatch("alert", { alertKind: "drop_tables", tradeDate: D }, ENV));
});

test("cleanDetail: one line, no mentions or backticks, bounded", () => {
  assert.equal(cleanDetail("a\nb `c` @someone"), "a b c someone");
  assert.equal(cleanDetail("x".repeat(500)).length, 300);
  assert.equal(cleanDetail(undefined), "");
});

test("the token never appears in Phase 1.1 logs or results", async () => {
  const { impl } = router([
    ["daily-data-update.yml/dispatches", () => new Response(`{"message":"bad ${TOKEN}"}`, { status: 401 })],
    ["pipeline-alert.yml/dispatches", accepted],
    ["/runs?", noRuns], ["sec_bhavdata", csvOk],
  ]);
  const { value, lines } = await quiet(() => run({ cron: STALE, scheduledTime: AT("15:05"), source: "cron" }, ENV, impl));
  assert.ok(!(JSON.stringify(value) + lines.join("\n")).includes(TOKEN));
});

// ---- dry run -------------------------------------------------------------

const post = (path, key) => new Request(`https://w.example${path}`, { method: "POST", headers: key ? { authorization: `Bearer ${key}` } : {} });

test("staleness dry run never dispatches or alerts, even when it would", async () => {
  const { impl, calls } = router([["/runs?", noRuns], ["sec_bhavdata", csvOk]]);
  const { value: res } = await quiet(() => handleFetch(post(`/__test-staleness?date=${D}&at=15:05`, KEY), ENV, impl));
  const body = await res.json();
  assert.deepEqual([body.dry_run, body.dispatched, body.alerted, body.decision.action], [true, false, false, "emergency_dispatch"]);
  assert.equal(posts(calls).length, 0);
});

test("staleness dry run requires the key", async () => {
  assert.equal((await handleFetch(post(`/__test-staleness?date=${D}`, "wrong"), ENV, async () => { throw new Error("no"); })).status, 401);
});

// zip helper kept referenced for the UDiFF-ready staleness case
test("staleness: UDiFF-only publication counts as published", async () => {
  const zip = () => new Response(makeFullZip("x.csv", `TradDt,BizDt\n${D},${D}\n`), { status: 200, headers: { "content-type": "application/zip" } });
  const { impl, calls } = router([["/dispatches", accepted], ["/runs?", noRuns], ["sec_bhavdata", notFound], ["BhavCopy", zip]]);
  await quiet(() => run({ cron: STALE, scheduledTime: AT("15:05"), source: "cron" }, ENV, impl));
  assert.deepEqual(postedTo(calls), ["daily-data-update.yml", "pipeline-alert.yml"]);
});
