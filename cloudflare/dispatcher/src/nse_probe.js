// Read-only Cloudflare -> NSE connectivity probe (Phase 1b follow-up).
//
// Fetches ONE allowlisted NSE resource per call - the same URLs the Python
// pipeline uses - and returns measurements only: HTTP status, latency, size,
// content type and a structural check of the payload. No market data is
// stored, forwarded or written anywhere, and nothing here is reachable from
// the cron handler: only the key-guarded test endpoint calls it.

// Same honest User-Agent as src/config.py. Never a spoofed browser string.
export const USER_AGENT =
  "indian-stock-research/0.1 (+personal research; contact via repository owner)";

const ARCHIVES = "https://nsearchives.nseindia.com";
const API = "https://www.nseindia.com/api";
const ALLOWED_HOSTS = new Set(["nsearchives.nseindia.com", "www.nseindia.com"]);

const ddmmyyyy = (d) => `${d.slice(8, 10)}${d.slice(5, 7)}${d.slice(0, 4)}`;
const yyyymmdd = (d) => d.replaceAll("-", "");

// name -> { url(date), kind, needsDate }
export const TARGETS = Object.freeze({
  universe: { kind: "csv", needsDate: false, url: () => `${ARCHIVES}/content/equities/EQUITY_L.csv` },
  delivery_bhavcopy: {
    kind: "csv",
    needsDate: true,
    url: (d) => `${ARCHIVES}/products/content/sec_bhavdata_full_${ddmmyyyy(d)}.csv`,
  },
  udiff_bhavcopy: {
    kind: "zip",
    needsDate: true,
    url: (d) => `${ARCHIVES}/content/cm/BhavCopy_NSE_CM_0_0_0_${yyyymmdd(d)}_F_0000.csv.zip`,
  },
  corporate_actions: { kind: "json", needsDate: false, url: () => `${API}/corporates-corporateActions?index=equities` },
});

/** Resolve a target name and optional date to a URL, or throw. Pure. */
export function resolveTarget(name, date) {
  if (!Object.hasOwn(TARGETS, name)) throw new Error("unknown target");
  const t = TARGETS[name];
  if (t.needsDate && !/^\d{4}-\d{2}-\d{2}$/.test(date ?? "")) throw new Error("date must be YYYY-MM-DD");
  const url = t.url(date);
  if (!ALLOWED_HOSTS.has(new URL(url).hostname)) throw new Error("host not allowed");
  return { name, kind: t.kind, url };
}

const firstLine = (text) => {
  const i = text.indexOf("\n");
  return (i === -1 ? text : text.slice(0, i)).replace(/\r$/, "").slice(0, 300);
};

function countLines(text) {
  let n = 0;
  for (let i = text.indexOf("\n"); i !== -1; i = text.indexOf("\n", i + 1)) n++;
  if (text.length && !text.endsWith("\n")) n++;
  return n;
}

// Signs that NSE's CDN served a challenge page instead of data.
const looksBlocked = (status, contentType, head) =>
  status === 403 ||
  status === 429 ||
  (/text\/html/i.test(contentType) && /access denied|reference #|captcha|challenge/i.test(head));

// Compressed size of the first entry, from the central directory.
function centralCompressedSize(dv) {
  for (let i = dv.byteLength - 22; i >= Math.max(0, dv.byteLength - 65557); i--) {
    if (dv.getUint32(i, true) === 0x06054b50) {
      const cd = dv.getUint32(i + 16, true);
      if (cd + 46 <= dv.byteLength && dv.getUint32(cd, true) === 0x02014b50) return dv.getUint32(cd + 20, true);
      return null;
    }
  }
  return null;
}

/** Structural check of a ZIP: local-header magic, first entry name, and the
 *  first decompressed line (the CSV header). Reads only the first entry. */
export async function inspectZip(bytes, { lines = 1 } = {}) {
  const u8 = new Uint8Array(bytes);
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  if (u8.length < 30 || dv.getUint32(0, true) !== 0x04034b50) return { valid: false };
  const flags = dv.getUint16(6, true);
  const method = dv.getUint16(8, true);
  let compSize = dv.getUint32(18, true);
  const nameLen = dv.getUint16(26, true);
  const extraLen = dv.getUint16(28, true);
  const entry = new TextDecoder().decode(u8.subarray(30, 30 + nameLen));
  const start = 30 + nameLen + extraLen;
  // Bit 3: sizes live in a trailing data descriptor; take them from the
  // central directory instead. The deflate input must end exactly where the
  // compressed data does - workerd rejects trailing bytes.
  if (flags & 0x08 || compSize === 0) compSize = centralCompressedSize(dv) ?? 0;
  let header = null;
  let firstRow = null;
  if (method === 8 && compSize > 0 && start + compSize <= u8.length) {
    const stream = new Blob([u8.subarray(start, start + compSize)]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    const reader = stream.getReader();
    let text = "";
    const dec = new TextDecoder();
    const newlines = () => text.split("\n").length - 1;
    while (newlines() < lines && text.length < 8192) {
      const { value, done } = await reader.read();
      if (done) break;
      text += dec.decode(value, { stream: true });
    }
    await reader.cancel().catch(() => {});
    header = firstLine(text);
    if (lines > 1) firstRow = (text.split("\n")[1] ?? "").replace(/\r$/, "").slice(0, 300);
  }
  const out = { valid: true, first_entry: entry, compression: method, csv_header: header };
  return lines > 1 ? { ...out, first_row: firstRow } : out;
}

/** Fetch one target and measure it. Never throws; failures are reported. */
export async function probe(target, { fetchImpl = fetch, now = () => Date.now() } = {}) {
  const out = { target: target.name, url: target.url, method: "GET" };
  const t0 = now();
  let res;
  try {
    res = await fetchImpl(target.url, { method: "GET", headers: { "User-Agent": USER_AGENT }, redirect: "manual" });
  } catch (err) {
    return { ...out, ok: false, status: 0, error: String(err?.message ?? err).slice(0, 200), total_ms: now() - t0 };
  }
  const headersMs = now() - t0;
  const body = await res.arrayBuffer();
  const totalMs = now() - t0;
  const contentType = res.headers.get("content-type") ?? "";
  const bytes = body.byteLength;
  const head = new TextDecoder().decode(new Uint8Array(body).subarray(0, 512));

  const result = {
    ...out,
    status: res.status,
    ok: res.status === 200,
    headers_ms: headersMs,
    total_ms: totalMs,
    bytes,
    content_type: contentType,
    server: res.headers.get("server"),
    location: res.headers.get("location"),
    blocked_suspected: looksBlocked(res.status, contentType, head),
  };
  if (res.status !== 200) {
    return { ...result, body_head: head.replace(/\s+/g, " ").slice(0, 160) };
  }

  try {
    if (target.kind === "csv") {
      const text = new TextDecoder().decode(body);
      const lines = countLines(text);
      return { ...result, data: { csv_header: firstLine(text), rows: Math.max(0, lines - 1) } };
    }
    if (target.kind === "zip") {
      return { ...result, data: await inspectZip(body) };
    }
    if (target.kind === "json") {
      const parsed = JSON.parse(new TextDecoder().decode(body));
      const records = Array.isArray(parsed) ? parsed : parsed?.data;
      return {
        ...result,
        data: {
          json_type: Array.isArray(parsed) ? "array" : typeof parsed,
          records: Array.isArray(records) ? records.length : null,
          first_record_keys: Array.isArray(records) && records[0] ? Object.keys(records[0]).slice(0, 12) : [],
        },
      };
    }
  } catch (err) {
    return { ...result, ok: false, parse_error: String(err?.message ?? err).slice(0, 200) };
  }
  return result;
}
