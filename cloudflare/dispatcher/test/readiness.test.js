import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { handleFetch, run } from "../src/index.js";
import {
  MAX_FAILED_RUNS,
  MIN_DELIVERY_ROWS,
  checkDelivery,
  checkUdiff,
  decide,
  evaluate,
  isFinalWindow,
  istDate,
  nseDate,
  readRuns,
  runTag,
  summariseRuns,
  validateDelivery,
} from "../src/readiness.js";
import { deliveryCsv, makeFullZip, quiet, router } from "./helpers.js";

const TOKEN = "github_pat_TESTONLY_readiness_0123456789";
const KEY = "k".repeat(64);
const PROD_CRON = "*/15 11-14 * * 1-5";
const ENV = Object.freeze({
  GITHUB_OWNER: "pjenil280505-max", GITHUB_REPO: "Indian-stock-market-", GITHUB_REF: "main",
  TEST_CRONS: "37 4 * * *", PRODUCTION_CRONS: PROD_CRON, PRODUCTION_ENABLED: "true",
  READINESS_FINAL_UTC: "14:45", GH_DISPATCH_TOKEN: TOKEN, TEST_TRIGGER_KEY: KEY,
});
const D = "2026-09-25";
const AT = (hhmm) => Date.parse(`${D}T${hhmm}:00Z`);

const runsBody = (runs) => () => new Response(JSON.stringify({ workflow_runs: runs }), { status: 200 });
const noRuns = runsBody([]);
const csv200 = (date = nseDate(D), rows = 3400) => () => new Response(deliveryCsv(date, rows), { status: 200 });
const zip200 = (date = D) => () =>
  new Response(makeFullZip("BhavCopy.csv", `TradDt,BizDt,Sgmt\n${date},${date},CM\n`), { status: 200 });
const notFound = () => new Response("<html>not found</html>", { status: 404 });
const dispatched = () => new Response(null, { status: 204 });

// ---- time handling: all UTC in, IST trade date out ----------------------

test("trade date is the IST date of the UTC fire time", () => {
  assert.equal(istDate(Date.parse("2026-09-25T11:45:00Z")), "2026-09-25");
  assert.equal(istDate(Date.parse("2026-09-24T18:29:59Z")), "2026-09-24"); // 23:59:59 IST
  assert.equal(istDate(Date.parse("2026-09-24T18:30:00Z")), "2026-09-25"); // 00:00 IST
});

test("NSE DATE1 format", () => {
  assert.equal(nseDate("2026-09-24"), "24-Sep-2026");
  assert.equal(nseDate("2026-01-05"), "05-Jan-2026");
});

test("final window is judged in UTC", () => {
  assert.equal(isFinalWindow(AT("14:30"), "14:45"), false);
  assert.equal(isFinalWindow(AT("14:45"), "14:45"), true);
  assert.equal(isFinalWindow(AT("11:00"), undefined), false);
});

// ---- the decision table --------------------------------------------------

const none = { total: 0, active: 0, succeeded: 0, failed: 0 };
const ready = { ready: true };
const notReady = { ready: false };

test("decide: dispatch complete when delivery file is ready", () => {
  assert.deepEqual(decide({ github: none, delivery: ready, udiff: null, finalWindow: false }),
    { action: "dispatch", completeness: "complete", reason: "delivery bhavcopy published" });
});

test("decide: skip when not published (not final window), even if UDiFF is out", () => {
  const d = decide({ github: none, delivery: notReady, udiff: ready, finalWindow: false });
  assert.equal(d.action, "skip");
  assert.equal(d.reason, "not published yet");
});

test("decide: final window accepts UDiFF-only as partial", () => {
  const d = decide({ github: none, delivery: notReady, udiff: ready, finalWindow: true });
  assert.deepEqual([d.action, d.completeness], ["dispatch", "partial"]);
});

test("decide: final window with nothing published skips", () => {
  assert.equal(decide({ github: none, delivery: notReady, udiff: notReady, finalWindow: true }).action, "skip");
});

test("decide: never a second dispatch while one is active or after success", () => {
  for (const github of [{ ...none, total: 1, active: 1 }, { ...none, total: 1, succeeded: 1 }]) {
    assert.equal(decide({ github, delivery: ready, udiff: ready, finalWindow: true }).action, "skip");
  }
});

test("decide: one failure allows a retry, the budget does not", () => {
  assert.equal(decide({ github: { ...none, failed: 1, total: 1 }, delivery: ready, finalWindow: false }).action, "dispatch");
  const d = decide({ github: { ...none, failed: MAX_FAILED_RUNS, total: 2 }, delivery: ready, finalWindow: false });
  assert.equal(d.action, "skip");
  assert.match(d.reason, /retry budget/);
});

test("decide: fails closed when GitHub state is unknown", () => {
  for (const github of [undefined, { error: "GitHub HTTP 500" }]) {
    assert.equal(decide({ github, delivery: ready, udiff: ready, finalWindow: true }).action, "skip");
  }
});

// ---- run summaries use the run-name tag ---------------------------------

test("only runs tagged for this trade date count", () => {
  const runs = [
    { display_title: `Daily data update (${runTag(D)} abc)`, status: "completed", conclusion: "success" },
    { display_title: `Daily data update (${runTag("2026-09-24")} x)`, status: "in_progress" },
    { display_title: "Daily data update (workflow_dispatch)", status: "in_progress" },
    { display_title: `Daily data update (${runTag(D)} def)`, status: "completed", conclusion: "failure" },
    { display_title: `Daily data update (${runTag(D)} ghi)`, status: "queued" },
  ];
  assert.deepEqual(summariseRuns(runs, D), { total: 3, active: 1, succeeded: 1, failed: 1 });
});

test("the daily workflow's run-name produces the tag the gate searches for", () => {
  const wf = readFileSync(new URL("../../../.github/workflows/daily-data-update.yml", import.meta.url), "utf8");
  assert.match(wf, /format\('Daily data update \(cloudflare \{0\} \{1\}\)', inputs\.trade_date, inputs\.request_id\)/);
  const rendered = `Daily data update (cloudflare ${D} some-uuid)`; // what GitHub renders
  assert.ok(rendered.includes(runTag(D)));
});

// ---- file validation -----------------------------------------------------

test("delivery file: ready only if dated right, has DELIV_PER, and is not truncated", () => {
  assert.deepEqual(validateDelivery(deliveryCsv(nseDate(D), 3400), D), { ready: true, rows: 3400, first_date: "25-Sep-2026" });
  assert.match(validateDelivery(deliveryCsv("24-Sep-2026", 3400), D).why, /file dated 24-Sep-2026/);
  assert.match(validateDelivery(deliveryCsv(nseDate(D), MIN_DELIVERY_ROWS - 1), D).why, /only/);
  assert.equal(validateDelivery("SYMBOL, SERIES, DATE1\nA, EQ, 25-Sep-2026\n", D).why, "unexpected header");
  assert.equal(validateDelivery("", D).why, "empty file");
});

test("checkDelivery reports 404 and network failures as not ready", async () => {
  assert.deepEqual(
    (await checkDelivery(D, router([["sec_bhavdata_full_25092026", notFound]]).impl)).ready, false);
  const r = await checkDelivery(D, async () => { throw new Error("ECONNRESET"); });
  assert.deepEqual([r.ready, r.status, r.why], [false, 0, "ECONNRESET"]);
});

test("checkUdiff validates the archive's first row date", async () => {
  const ok = await checkUdiff(D, router([["BhavCopy_NSE_CM_0_0_0_20260925", zip200()]]).impl);
  assert.equal(ok.ready, true);
  const stale = await checkUdiff(D, router([["BhavCopy_NSE_CM_0_0_0_20260925", zip200("2026-09-24")]]).impl);
  assert.equal(stale.ready, false);
  assert.match(stale.why, /2026-09-24/);
});

// ---- GitHub read ---------------------------------------------------------

test("readRuns is a GET, filters by date, and sends the token only to GitHub", async () => {
  const { impl, calls } = router([["api.github.com", noRuns]]);
  assert.deepEqual(await readRuns(D, ENV, impl), { total: 0, active: 0, succeeded: 0, failed: 0 });
  assert.equal(calls[0].init.method, "GET");
  assert.match(calls[0].url, /daily-data-update\.yml\/runs\?event=workflow_dispatch&created=%3E%3D2026-09-25/);
  assert.equal(calls[0].init.headers.Authorization, `Bearer ${TOKEN}`);
});

test("readRuns turns HTTP errors into an error, never a guess", async () => {
  const r = await readRuns(D, ENV, router([["api.github.com", () => new Response("no", { status: 403 })]]).impl);
  assert.deepEqual(r, { error: "GitHub HTTP 403" });
});

// ---- evaluate: order and early exit -------------------------------------

test("NSE is not contacted when today's run already succeeded", async () => {
  const { impl, calls } = router([
    ["api.github.com", runsBody([{ display_title: `x (${runTag(D)} a)`, status: "completed", conclusion: "success" }])],
  ]);
  const r = await evaluate({ scheduledTime: AT("12:00"), env: ENV, fetchImpl: impl });
  assert.equal(r.decision.action, "skip");
  assert.equal(calls.length, 1);
  assert.equal(r.delivery, null);
});

test("UDiFF is only fetched in the final window", async () => {
  const early = router([["api.github.com", noRuns], ["sec_bhavdata_full", notFound]]);
  await evaluate({ scheduledTime: AT("12:00"), env: ENV, fetchImpl: early.impl });
  assert.equal(early.calls.filter((c) => c.url.includes("BhavCopy")).length, 0);

  const late = router([["api.github.com", noRuns], ["sec_bhavdata_full", notFound], ["BhavCopy", zip200()]]);
  const r = await evaluate({ scheduledTime: AT("14:45"), env: ENV, fetchImpl: late.impl });
  assert.equal(r.decision.completeness, "partial");
});

// ---- the production path in the Worker ----------------------------------

test("production fire before publication dispatches nothing", async () => {
  const { impl, calls } = router([["api.github.com/repos", noRuns], ["sec_bhavdata_full", notFound]]);
  const { value } = await quiet(() => run({ cron: PROD_CRON, scheduledTime: AT("11:00"), source: "cron" }, ENV, impl));
  assert.equal(value.skipped, true);
  assert.equal(value.reason, "not published yet");
  assert.ok(!calls.some((c) => c.url.endsWith("/dispatches")));
});

test("production fire after publication dispatches the daily workflow once, tagged", async () => {
  const { impl, calls } = router([
    ["/dispatches", dispatched],
    ["/runs?", noRuns],
    ["sec_bhavdata_full_25092026", csv200()],
  ]);
  const { value } = await quiet(() => run({ cron: PROD_CRON, scheduledTime: AT("11:45"), source: "cron" }, ENV, impl));
  assert.equal(value.ok, true);
  const posts = calls.filter((c) => c.url.endsWith("/dispatches"));
  assert.equal(posts.length, 1);
  assert.match(posts[0].url, /daily-data-update\.yml\/dispatches$/);
  const body = JSON.parse(posts[0].init.body);
  assert.deepEqual(Object.keys(body.inputs).sort(), ["catchup_days", "request_id", "trade_date"]);
  assert.equal(body.inputs.trade_date, D);
  assert.equal(body.inputs.request_id, value.request_id);
});

test("production fire does not dispatch when GitHub cannot be read", async () => {
  const { impl, calls } = router([["/runs?", () => new Response("", { status: 500 })], ["sec_bhavdata_full", csv200()]]);
  const { value } = await quiet(() => run({ cron: PROD_CRON, scheduledTime: AT("11:45"), source: "cron" }, ENV, impl));
  assert.equal(value.skipped, true);
  assert.equal(value.reason, "github state unknown");
  assert.ok(!calls.some((c) => c.url.endsWith("/dispatches") || c.url.includes("nseindia")));
});

test("with production disabled, a production cron never reaches the gate or NSE", async () => {
  const off = { ...ENV, PRODUCTION_ENABLED: "false" };
  const { impl, calls } = router([]);
  const { value } = await quiet(() => run({ cron: PROD_CRON, scheduledTime: AT("11:45"), source: "cron" }, off, impl));
  assert.equal(value.kind, "blocked");
  assert.equal(calls.length, 0);
});

test("the token never appears in readiness logs or results", async () => {
  const { impl } = router([["/runs?", noRuns], ["sec_bhavdata_full", notFound]]);
  const { value, lines } = await quiet(() => run({ cron: PROD_CRON, scheduledTime: AT("11:00"), source: "cron" }, ENV, impl));
  assert.ok(!(JSON.stringify(value) + lines.join("\n")).includes(TOKEN));
});

// ---- dry-run endpoint ----------------------------------------------------

const post = (path, key) => new Request(`https://w.example${path}`, { method: "POST", headers: key ? { authorization: `Bearer ${key}` } : {} });

test("readiness dry run never dispatches, even when the gate says dispatch", async () => {
  const { impl, calls } = router([["/runs?", noRuns], ["sec_bhavdata_full_25092026", csv200()]]);
  const { value: res } = await quiet(() => handleFetch(post(`/__test-readiness?date=${D}&at=11:45`, KEY), ENV, impl));
  const body = await res.json();
  assert.equal(res.status, 200);
  assert.deepEqual([body.dry_run, body.dispatched, body.decision.action], [true, false, "dispatch"]);
  assert.ok(!calls.some((c) => c.url.endsWith("/dispatches")));
});

test("readiness dry run requires the key and valid parameters", async () => {
  const f = async () => { throw new Error("must not fetch"); };
  assert.equal((await handleFetch(post(`/__test-readiness?date=${D}`, "wrong"), ENV, f)).status, 401);
  assert.equal((await handleFetch(post("/__test-readiness?date=25-09-2026", KEY), ENV, f)).status, 400);
  assert.equal((await handleFetch(post(`/__test-readiness?date=${D}&at=9pm`, KEY), ENV, f)).status, 400);
});
