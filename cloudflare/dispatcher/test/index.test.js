import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import worker, {
  WORKFLOWS,
  buildDispatch,
  classify,
  dispatch,
  handleFetch,
  redact,
  run,
} from "../src/index.js";

const TOKEN = "github_pat_TESTONLY_0123456789abcdefghijklmnopqrstuvwxyz";
const TEST_CRON = "37 4 * * *";
const ENV = Object.freeze({
  GITHUB_OWNER: "pjenil280505-max",
  GITHUB_REPO: "Indian-stock-market-",
  GITHUB_REF: "main",
  TEST_CRONS: TEST_CRON,
  PRODUCTION_CRONS: "",
  PRODUCTION_ENABLED: "false",
  GH_DISPATCH_TOKEN: TOKEN,
});

function fakeFetch(responses) {
  const calls = [];
  const queue = [...responses];
  const impl = async (url, init) => {
    calls.push({ url, init });
    const next = queue.shift();
    if (next instanceof Error) throw next;
    const { status, body = "" } = next;
    return new Response(status === 204 ? null : typeof body === "string" ? body : JSON.stringify(body), { status });
  };
  return { impl, calls };
}

function captureLogs(fn) {
  const lines = [];
  const orig = { log: console.log, error: console.error };
  console.log = (...a) => lines.push(a.join(" "));
  console.error = (...a) => lines.push(a.join(" "));
  return Promise.resolve(fn()).then(
    (v) => { Object.assign(console, orig); return { value: v, lines }; },
    (e) => { Object.assign(console, orig); throw e; },
  );
}

// ---- classification: production must be unreachable in Phase 1b ----------

test("the configured test cron is a TEST invocation", () => {
  assert.deepEqual(classify(TEST_CRON, ENV), { kind: "test" });
});

test("an unknown cron dispatches nothing", () => {
  assert.equal(classify("0 0 * * *", ENV).kind, "ignored");
});

test("a production cron is blocked unless production is explicitly enabled", () => {
  const env = { ...ENV, PRODUCTION_CRONS: "41 11 * * 1-5" };
  assert.equal(classify("41 11 * * 1-5", env).kind, "blocked");
  assert.equal(classify("41 11 * * 1-5", { ...env, PRODUCTION_ENABLED: "TRUE" }).kind, "blocked");
  assert.equal(classify("41 11 * * 1-5", { ...env, PRODUCTION_ENABLED: "true" }).kind, "production");
});

test("the committed wrangler.toml has exactly the approved production config", () => {
  const toml = readFileSync(new URL("../wrangler.toml", import.meta.url), "utf8");
  assert.match(toml, /^PRODUCTION_ENABLED = "true"$/m);
  assert.match(toml, /^PRODUCTION_CRONS = "\*\/15 11-14 \* \* 1-5"$/m);
  const crons = [...toml.matchAll(/^crons = \[(.*)\]$/gm)].map((m) => m[1]);
  assert.deepEqual(crons, ['"37 4 * * *", "*/15 11-14 * * 1-5"']);
  const env = { PRODUCTION_CRONS: "*/15 11-14 * * 1-5", PRODUCTION_ENABLED: "true", TEST_CRONS: "37 4 * * *" };
  assert.equal(classify("*/15 11-14 * * 1-5", env).kind, "production");
  assert.equal(classify("37 4 * * *", env).kind, "test");
});

// ---- request shape -------------------------------------------------------

test("test dispatch targets the no-op probe with its declared inputs", () => {
  const req = buildDispatch("test", { cron: TEST_CRON, scheduledTime: "2026-09-25T04:37:00.000Z", requestId: "r1" }, ENV);
  assert.equal(req.workflow, "cloudflare-dispatch-probe.yml");
  assert.equal(
    req.url,
    "https://api.github.com/repos/pjenil280505-max/Indian-stock-market-/actions/workflows/cloudflare-dispatch-probe.yml/dispatches",
  );
  assert.deepEqual(req.body, {
    ref: "main",
    inputs: { invocation: "test", cron: TEST_CRON, scheduled_time: "2026-09-25T04:37:00.000Z", request_id: "r1" },
  });
});

test("only the two known workflows can ever be targeted", () => {
  assert.deepEqual(Object.values(WORKFLOWS).sort(), ["cloudflare-dispatch-probe.yml", "daily-data-update.yml"]);
  for (const kind of ["migrate", "backfill", "blocked", "ignored", "__proto__", "constructor"]) {
    assert.throws(() => buildDispatch(kind, {}, ENV));
  }
});

test("production inputs match daily-data-update.yml exactly", () => {
  const wf = readFileSync(new URL("../../../.github/workflows/daily-data-update.yml", import.meta.url), "utf8");
  const req = buildDispatch("production", {}, ENV);
  for (const key of Object.keys(req.body.inputs)) assert.match(wf, new RegExp(`^      ${key}:$`, "m"));
});

test("probe inputs match cloudflare-dispatch-probe.yml exactly", () => {
  const wf = readFileSync(new URL("../../../.github/workflows/cloudflare-dispatch-probe.yml", import.meta.url), "utf8");
  const req = buildDispatch("test", { cron: "c", scheduledTime: "t", requestId: "r" }, ENV);
  for (const key of Object.keys(req.body.inputs)) assert.match(wf, new RegExp(`^      ${key}:$`, "m"));
});

test("the GitHub API base cannot be redirected off loopback", () => {
  assert.throws(() => buildDispatch("test", {}, { ...ENV, GITHUB_API_BASE: "https://evil.example" }));
  assert.throws(() => buildDispatch("test", {}, { ...ENV, GITHUB_API_BASE: "http://127.0.0.1.evil.example" }));
  assert.ok(buildDispatch("test", {}, { ...ENV, GITHUB_API_BASE: "http://127.0.0.1:9999" }).url.startsWith("http://127.0.0.1:9999/"));
});

// ---- dispatch ------------------------------------------------------------

test("200 response yields the GitHub run id", async () => {
  const { impl, calls } = fakeFetch([{ status: 200, body: { workflow_run_id: 42, html_url: "https://github.com/x/runs/42" } }]);
  const req = buildDispatch("test", { cron: TEST_CRON, scheduledTime: "t", requestId: "r" }, ENV);
  const res = await dispatch(req, ENV, impl);
  assert.deepEqual(res, { ok: true, status: 200, attempt: 1, workflow_run_id: 42, run_url: "https://github.com/x/runs/42" });
  assert.equal(calls[0].init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(calls[0].init.headers["User-Agent"], "nse-pipeline-dispatcher");
});

test("legacy 204 response is also success", async () => {
  const { impl } = fakeFetch([{ status: 204 }]);
  const res = await dispatch(buildDispatch("test", {}, ENV), ENV, impl);
  assert.equal(res.ok, true);
  assert.equal(res.workflow_run_id, null);
});

test("4xx is not retried and reports GitHub's message", async () => {
  const { impl, calls } = fakeFetch([{ status: 403, body: { message: "Resource not accessible by personal access token" } }]);
  const res = await dispatch(buildDispatch("test", {}, ENV), ENV, impl);
  assert.equal(calls.length, 1);
  assert.deepEqual(res, { ok: false, status: 403, attempt: 1, error: "Resource not accessible by personal access token" });
});

test("5xx is retried once", async () => {
  const { impl, calls } = fakeFetch([{ status: 502, body: "bad gateway" }, { status: 204 }]);
  const res = await dispatch(buildDispatch("test", {}, ENV), ENV, impl);
  assert.equal(calls.length, 2);
  assert.equal(res.ok, true);
  assert.equal(res.attempt, 2);
});

test("missing token fails without any request", async () => {
  const { impl, calls } = fakeFetch([]);
  const res = await dispatch(buildDispatch("test", {}, ENV), { ...ENV, GH_DISPATCH_TOKEN: "" }, impl);
  assert.equal(calls.length, 0);
  assert.equal(res.ok, false);
});

// ---- nothing sensitive leaves the Worker --------------------------------

test("the token never appears in logs or results, even if GitHub echoes it", async () => {
  const { impl } = fakeFetch([{ status: 401, body: { message: `Bad credentials for ${TOKEN}` } }]);
  const { value, lines } = await captureLogs(() =>
    run({ cron: TEST_CRON, scheduledTime: Date.UTC(2026, 8, 25, 4, 37), source: "cron" }, ENV, impl),
  );
  const everything = JSON.stringify(value) + lines.join("\n");
  assert.ok(!everything.includes(TOKEN));
  assert.ok(!everything.includes("github_pat_"));
  assert.match(value.error, /\[redacted\]/);
});

test("redact truncates long messages", () => {
  assert.equal(redact("x".repeat(500), TOKEN).length, 200);
});

test("scheduled time is logged in UTC", async () => {
  const { impl } = fakeFetch([{ status: 204 }]);
  const { value } = await captureLogs(() =>
    run({ cron: TEST_CRON, scheduledTime: Date.UTC(2026, 8, 25, 4, 37), source: "cron" }, ENV, impl),
  );
  assert.equal(value.scheduled_time_utc, "2026-09-25T04:37:00.000Z");
  assert.equal(value.kind, "test");
  assert.equal(value.source, "cron");
});

test("scheduled handler throws on failed dispatch so Cloudflare records a failure", async () => {
  const origFetch = globalThis.fetch;
  globalThis.fetch = fakeFetch([{ status: 404, body: { message: "Not Found" } }]).impl;
  try {
    await captureLogs(() =>
      assert.rejects(worker.scheduled({ cron: TEST_CRON, scheduledTime: Date.now() }, ENV), /status 404/),
    );
  } finally {
    globalThis.fetch = origFetch;
  }
});

// ---- HTTP surface --------------------------------------------------------

const post = (path, key) =>
  new Request(`https://w.example${path}`, { method: "POST", headers: key ? { authorization: `Bearer ${key}` } : {} });

test("every path is 404 when no test key is configured", async () => {
  for (const req of [post("/__test-dispatch", "anything"), new Request("https://w.example/"), post("/")]) {
    assert.equal((await handleFetch(req, ENV)).status, 404);
  }
});

test("wrong or missing key is rejected without dispatching", async () => {
  const env = { ...ENV, TEST_TRIGGER_KEY: "k".repeat(64) };
  const { impl, calls } = fakeFetch([]);
  assert.equal((await handleFetch(post("/__test-dispatch", "wrong"), env, impl)).status, 401);
  assert.equal((await handleFetch(post("/__test-dispatch"), env, impl)).status, 401);
  assert.equal(calls.length, 0);
});

test("the test endpoint can only dispatch the no-op probe, even with production enabled", async () => {
  const env = { ...ENV, TEST_TRIGGER_KEY: "k".repeat(64), PRODUCTION_ENABLED: "true", PRODUCTION_CRONS: "manual-test" };
  const { impl, calls } = fakeFetch([{ status: 200, body: { workflow_run_id: 7 } }]);
  const { value: res } = await captureLogs(() => handleFetch(post("/__test-dispatch", "k".repeat(64)), env, impl));
  assert.equal(res.status, 200);
  assert.match(calls[0].url, /cloudflare-dispatch-probe\.yml/);
  const body = await res.json();
  assert.equal(body.workflow_run_id, 7);
  assert.equal(body.kind, "test");
  assert.ok(!JSON.stringify(body).includes("k".repeat(64)));
});

test("the auth-check endpoint never dispatches", async () => {
  const env = { ...ENV, TEST_TRIGGER_KEY: "k".repeat(64) };
  const { impl, calls } = fakeFetch([]);
  assert.equal((await handleFetch(post("/__test-auth", "k".repeat(64)), env, impl)).status, 204);
  assert.equal((await handleFetch(post("/__test-auth", "wrong"), env, impl)).status, 401);
  assert.equal((await handleFetch(post("/__test-auth", "k".repeat(64)), ENV, impl)).status, 404);
  assert.equal(calls.length, 0);
});
