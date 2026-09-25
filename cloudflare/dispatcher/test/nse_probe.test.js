import assert from "node:assert/strict";
import { test } from "node:test";
import { deflateRawSync } from "node:zlib";

import worker, { handleFetch } from "../src/index.js";
import { TARGETS, USER_AGENT, inspectZip, probe, resolveTarget } from "../src/nse_probe.js";

const KEY = "k".repeat(64);
const ENV = Object.freeze({
  GITHUB_OWNER: "o", GITHUB_REPO: "r", GITHUB_REF: "main",
  TEST_CRONS: "37 4 * * *", PRODUCTION_CRONS: "", PRODUCTION_ENABLED: "false",
  GH_DISPATCH_TOKEN: "github_pat_TESTONLY_x", TEST_TRIGGER_KEY: KEY,
});

// A complete archive: local header + data + central directory + EOCD, the
// way NSE's files are laid out. descriptor=true puts sizes only in the
// central directory (general-purpose flag bit 3).
function makeFullZip(name, content, { descriptor = false } = {}) {
  const data = deflateRawSync(Buffer.from(content));
  const nameBuf = Buffer.from(name);
  const local = Buffer.alloc(30);
  local.writeUInt32LE(0x04034b50, 0);
  local.writeUInt16LE(20, 4);
  local.writeUInt16LE(descriptor ? 0x08 : 0, 6);
  local.writeUInt16LE(8, 8);
  local.writeUInt32LE(descriptor ? 0 : data.length, 18);
  local.writeUInt32LE(descriptor ? 0 : content.length, 22);
  local.writeUInt16LE(nameBuf.length, 26);
  const dd = descriptor ? Buffer.alloc(16) : Buffer.alloc(0);
  if (descriptor) { dd.writeUInt32LE(0x08074b50, 0); dd.writeUInt32LE(data.length, 8); dd.writeUInt32LE(content.length, 12); }
  const cd = Buffer.alloc(46);
  cd.writeUInt32LE(0x02014b50, 0);
  cd.writeUInt16LE(8, 10);
  cd.writeUInt32LE(data.length, 20);
  cd.writeUInt32LE(content.length, 24);
  cd.writeUInt16LE(nameBuf.length, 28);
  const cdOffset = local.length + nameBuf.length + data.length + dd.length;
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(1, 8);
  eocd.writeUInt16LE(1, 10);
  eocd.writeUInt32LE(cd.length + nameBuf.length, 12);
  eocd.writeUInt32LE(cdOffset, 16);
  return Buffer.concat([local, nameBuf, data, dd, cd, nameBuf, eocd]);
}

function makeZip(name, content) {
  const data = deflateRawSync(Buffer.from(content));
  const nameBuf = Buffer.from(name);
  const h = Buffer.alloc(30);
  h.writeUInt32LE(0x04034b50, 0);
  h.writeUInt16LE(20, 4);
  h.writeUInt16LE(8, 8); // deflate
  h.writeUInt32LE(data.length, 18);
  h.writeUInt32LE(content.length, 22);
  h.writeUInt16LE(nameBuf.length, 26);
  h.writeUInt16LE(0, 28);
  return Buffer.concat([h, nameBuf, data]);
}

const respond = (status, body, headers = {}) => async () => new Response(body, { status, headers });

// ---- target allowlist ----------------------------------------------------

test("targets are exactly the four resources the Python pipeline reads", () => {
  assert.deepEqual(Object.keys(TARGETS).sort(), ["corporate_actions", "delivery_bhavcopy", "udiff_bhavcopy", "universe"]);
  assert.equal(resolveTarget("universe").url, "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv");
  assert.equal(
    resolveTarget("delivery_bhavcopy", "2026-09-24").url,
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_24092026.csv",
  );
  assert.equal(
    resolveTarget("udiff_bhavcopy", "2026-09-24").url,
    "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260924_F_0000.csv.zip",
  );
  assert.equal(resolveTarget("corporate_actions").url, "https://www.nseindia.com/api/corporates-corporateActions?index=equities");
});

test("unknown targets, prototype names and bad dates are refused", () => {
  for (const name of ["upstox", "constructor", "__proto__", "", null]) assert.throws(() => resolveTarget(name));
  for (const d of [undefined, "", "24-09-2026", "2026-9-24", "2026-09-24/../x"]) {
    assert.throws(() => resolveTarget("delivery_bhavcopy", d));
  }
});

test("every target URL is on an NSE host and is fetched with GET only", async () => {
  for (const name of Object.keys(TARGETS)) {
    const t = resolveTarget(name, "2026-09-24");
    assert.match(new URL(t.url).hostname, /^(nsearchives|www)\.nseindia\.com$/);
    let seen;
    await probe(t, { fetchImpl: async (u, init) => { seen = init; return new Response("", { status: 404 }); } });
    assert.equal(seen.method, "GET");
    assert.equal(seen.headers["User-Agent"], USER_AGENT);
    assert.ok(!/Mozilla|Chrome|Safari/.test(USER_AGENT), "never a spoofed browser UA");
  }
});

// ---- measurements --------------------------------------------------------

test("CSV: header and row count, no data returned", async () => {
  const csv = "SYMBOL,NAME OF COMPANY, SERIES\nAAA,A Ltd,EQ\nBBB,B Ltd,EQ\n";
  const r = await probe(resolveTarget("universe"), { fetchImpl: respond(200, csv, { "content-type": "text/csv" }) });
  assert.equal(r.ok, true);
  assert.deepEqual(r.data, { csv_header: "SYMBOL,NAME OF COMPANY, SERIES", rows: 2 });
  assert.equal(r.bytes, csv.length);
  assert.ok(!JSON.stringify(r).includes("A Ltd"), "rows themselves are never returned");
});

test("ZIP: magic, entry name and decompressed CSV header", async () => {
  const zip = makeZip("BhavCopy_NSE_CM_0_0_0_20260924_F_0000.csv", "TradDt,BizDt,Sgmt\n2026-09-24,2026-09-24,CM\n");
  const info = await inspectZip(zip);
  assert.deepEqual(info, {
    valid: true, first_entry: "BhavCopy_NSE_CM_0_0_0_20260924_F_0000.csv", compression: 8, csv_header: "TradDt,BizDt,Sgmt",
  });
  assert.deepEqual(await inspectZip(Buffer.from("<html>nope</html>")), { valid: false });
});

test("JSON: record count and field names", async () => {
  const body = JSON.stringify([{ symbol: "AAA", subject: "Dividend", exDate: "01-Oct-2026" }]);
  const r = await probe(resolveTarget("corporate_actions"), { fetchImpl: respond(200, body, { "content-type": "application/json" }) });
  assert.deepEqual(r.data, { json_type: "array", records: 1, first_record_keys: ["symbol", "subject", "exDate"] });
});

test("404 is reported as not available, not as blocked", async () => {
  const r = await probe(resolveTarget("delivery_bhavcopy", "2026-09-25"), { fetchImpl: respond(404, "Not Found") });
  assert.equal(r.ok, false);
  assert.equal(r.status, 404);
  assert.equal(r.blocked_suspected, false);
});

test("403 and CDN challenge pages are flagged as blocked", async () => {
  const r403 = await probe(resolveTarget("universe"), { fetchImpl: respond(403, "Access Denied") });
  assert.equal(r403.blocked_suspected, true);
  const page = "<html><title>Access Denied</title>Reference #18.abc</html>";
  const r200 = await probe(resolveTarget("universe"), { fetchImpl: respond(200, page, { "content-type": "text/html" }) });
  assert.equal(r200.blocked_suspected, true);
});

test("network failure is reported, not thrown", async () => {
  const r = await probe(resolveTarget("universe"), { fetchImpl: async () => { throw new Error("connect ECONNRESET"); } });
  assert.deepEqual([r.ok, r.status, r.error], [false, 0, "connect ECONNRESET"]);
});

test("redirects are recorded, not followed", async () => {
  const r = await probe(resolveTarget("universe"), { fetchImpl: respond(302, "", { location: "https://www.nseindia.com/" }) });
  assert.equal(r.status, 302);
  assert.equal(r.location, "https://www.nseindia.com/");
});

// ---- reachability: key-guarded endpoint only, never the cron ------------

const post = (path, key) => new Request(`https://w.example${path}`, { method: "POST", headers: key ? { authorization: `Bearer ${key}` } : {} });

test("NSE probe endpoint requires the test key", async () => {
  let calls = 0;
  const f = async () => { calls++; return new Response("x"); };
  assert.equal((await handleFetch(post("/__test-nse?target=universe", "wrong"), ENV, f)).status, 401);
  assert.equal((await handleFetch(post("/__test-nse?target=universe"), { ...ENV, TEST_TRIGGER_KEY: "" }, f)).status, 404);
  assert.equal(calls, 0);
});

test("NSE probe endpoint rejects targets outside the allowlist", async () => {
  const res = await handleFetch(post("/__test-nse?target=upstox", KEY), ENV, async () => { throw new Error("must not fetch"); });
  assert.equal(res.status, 400);
});

test("NSE probe endpoint never dispatches a GitHub workflow", async () => {
  const urls = [];
  const f = async (u) => { urls.push(String(u)); return new Response("A,B\n1,2\n", { status: 200 }); };
  const orig = console.log; console.log = () => {};
  try {
    const res = await handleFetch(post("/__test-nse?target=universe", KEY), ENV, f);
    assert.equal(res.status, 200);
  } finally { console.log = orig; }
  assert.deepEqual(urls, ["https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"]);
});

test("the cron handler never contacts NSE", async () => {
  const urls = [];
  const orig = { fetch: globalThis.fetch, log: console.log };
  globalThis.fetch = async (u) => { urls.push(String(u)); return new Response(null, { status: 204 }); };
  console.log = () => {};
  try {
    await worker.scheduled({ cron: "37 4 * * *", scheduledTime: Date.now() }, ENV);
  } finally { globalThis.fetch = orig.fetch; console.log = orig.log; }
  assert.equal(urls.length, 1);
  assert.match(urls[0], /^https:\/\/api\.github\.com\//);
});

test("ZIP with a central directory after the data (real layout) is read correctly", async () => {
  for (const descriptor of [false, true]) {
    const zip = makeFullZip("x.csv", "TradDt,BizDt\n2026-09-24,2026-09-24\n", { descriptor });
    const info = await inspectZip(zip);
    assert.equal(info.csv_header, "TradDt,BizDt", `descriptor=${descriptor}`);
  }
});
