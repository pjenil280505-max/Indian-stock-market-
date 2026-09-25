import { deflateRawSync } from "node:zlib";

// A complete single-entry ZIP (local header, data, central directory, EOCD),
// laid out like NSE's archives.
export function makeFullZip(name, content) {
  const data = deflateRawSync(Buffer.from(content));
  const nameBuf = Buffer.from(name);
  const local = Buffer.alloc(30);
  local.writeUInt32LE(0x04034b50, 0);
  local.writeUInt16LE(20, 4);
  local.writeUInt16LE(8, 8);
  local.writeUInt32LE(data.length, 18);
  local.writeUInt32LE(content.length, 22);
  local.writeUInt16LE(nameBuf.length, 26);
  const cd = Buffer.alloc(46);
  cd.writeUInt32LE(0x02014b50, 0);
  cd.writeUInt16LE(8, 10);
  cd.writeUInt32LE(data.length, 20);
  cd.writeUInt32LE(content.length, 24);
  cd.writeUInt16LE(nameBuf.length, 28);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(1, 8);
  eocd.writeUInt16LE(1, 10);
  eocd.writeUInt32LE(cd.length + nameBuf.length, 12);
  eocd.writeUInt32LE(local.length + nameBuf.length + data.length, 16);
  return Buffer.concat([local, nameBuf, data, cd, nameBuf, eocd]);
}

// Build a delivery bhavcopy with `rows` data rows dated `nseDate`.
export function deliveryCsv(nseDate, rows) {
  const header =
    "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER";
  const body = Array.from({ length: rows }, (_, i) =>
    `SYM${i}, EQ, ${nseDate}, 10, 10, 11, 9, 10, 10, 10, 100, 1, 5, 50, 50.00`);
  return [header, ...body].join("\n") + "\n";
}

// A fake fetch routed by URL substring; records every call.
export function router(routes) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url: String(url), init });
    for (const [needle, make] of routes) {
      if (String(url).includes(needle)) return make();
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  return { impl, calls };
}

export const quiet = async (fn) => {
  const orig = { log: console.log, error: console.error };
  const lines = [];
  console.log = (...a) => lines.push(a.join(" "));
  console.error = (...a) => lines.push(a.join(" "));
  try {
    return { value: await fn(), lines };
  } finally {
    Object.assign(console, orig);
  }
};
