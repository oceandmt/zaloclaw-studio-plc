#!/usr/bin/env node
// Raw LISTEN-ONLY capture for one Zalo group. Logs every incoming message as
// one JSON line (NDJSON) incl. the raw `quote` block, so we can inspect the
// real wire shape of replies without sending anything.
//
// Usage: node scripts/zcapture.mjs <groupId> <seconds> <outFile> [account]
//   node scripts/zcapture.mjs <DST_GROUP_ID> 3600 data/capture_raw.ndjson nick1
//
// Safety: creates NO send path at all. Auto-reconnects if the socket drops.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Zalo } from "zca-js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const [gid, secsArg, outArg, accountArg] = process.argv.slice(2);
if (!gid) { console.error("usage: node scripts/zcapture.mjs <groupId> <seconds> <outFile> [account]"); process.exit(2); }
const SECS = parseInt(secsArg || "3600", 10);
const OUT = path.resolve(ROOT, outArg || "data/capture_raw.ndjson");
const ACCOUNT = accountArg || "nick1";
const CREDS = JSON.parse(fs.readFileSync(path.join(ROOT, "data/accounts", `${ACCOUNT}.json`), "utf8"));

const started = Date.now();
let listener = null, api = null, reconnects = 0;
function logLine(obj) { fs.appendFileSync(OUT, JSON.stringify(obj) + "\n"); }

function shape(message) {
  const d = message?.data || {};
  const content = (d.content && typeof d.content === "object") ? d.content : String(d.content ?? "");
  return {
    ev: "msg",
    at: Date.now(),
    isSelf: !!message?.isSelf,
    group: message?.threadId || d.idTo || "",
    msgId: d.msgId ?? null,
    realMsgId: d.realMsgId ?? null,
    cliMsgId: d.cliMsgId ?? null,
    uidFrom: d.uidFrom ?? null,
    dName: d.dName ?? null,
    ts: d.ts ?? null,
    msgType: d.msgType ?? null,
    hasQuote: !!d.quote,
    quote: d.quote ? {
      ownerId: d.quote.ownerId ?? null, cliMsgId: d.quote.cliMsgId ?? null,
      msgId: d.quote.msgId ?? null, globalMsgId: d.quote.globalMsgId ?? null,
      cliMsgType: d.quote.cliMsgType ?? null, ts: d.quote.ts ?? null,
      msg: typeof d.quote.msg === "string" ? d.quote.msg.slice(0, 120) : d.quote.msg ?? null,
      attach: typeof d.quote.attach === "string" ? d.quote.attach.slice(0, 60) : d.quote.attach ?? null,
      fromD: d.quote.fromD ?? null, ttl: d.quote.ttl ?? null,
    } : null,
    content: typeof content === "string" ? content.slice(0, 200) : content,
    keys: Object.keys(d),
  };
}

async function connect() {
  const zalo = new Zalo({ selfListen: true, logging: false, checkUpdate: false });
  api = await zalo.login({ imei: CREDS.imei, cookie: CREDS.cookie, userAgent: CREDS.userAgent, language: CREDS.language || "vi" });
  listener = api.listener;
  listener.on("message", (m) => {
    try {
      const s = shape(m);
      if (String(s.group) !== String(gid)) return;
      logLine(s);
      console.log("MSG", s.hasQuote ? "QUOTE" : "flat", s.msgId, s.uidFrom, (typeof s.content === "string" ? s.content.slice(0, 50) : ""));
    } catch (e) { console.log("EVT-ERR", String(e?.message || e)); }
  });
  listener.on("error", async (e) => {
    console.log("LISTEN-ERR", String(e?.message || e));
    if (Date.now() - started < SECS * 1000) { reconnects++; setTimeout(() => connect().catch(() => {}), 5000); }
  });
  listener.start();
  logLine({ ev: "start", at: Date.now(), account: ACCOUNT, gid, ownId: (() => { try { return api.getOwnId?.() ?? null; } catch { return null; } })(), reconnects });
  console.log("listening... gid=", gid, "out=", OUT);
}

await connect();
setTimeout(() => { try { listener?.stop(); } catch {} logLine({ ev: "done", at: Date.now(), reconnects }); console.log("done"); process.exit(0); }, SECS * 1000);
