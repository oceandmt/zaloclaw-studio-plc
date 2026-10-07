// Autobot: a long-lived, LISTEN-ONLY worker per Zalo account.
//
// It opens the zca-js websocket listener, streams every NEW message it sees as
// NDJSON on stdout, and (for forward rules) sends into a destination group when
// the controller asks it to. There is deliberately NO auto-reply logic here —
// the bot only ever relays what a human posted in another group.
//
// Protocol (stdin, one JSON per line):
//   {"id":1,"cmd":"ping"}
//   {"id":2,"cmd":"switch-to","account":"nick2"}    -> open/listen on that acct
//   {"id":3,"cmd":"send","thread":"<gid>","type":"group","text":"..."[, "dryRun":true]}
//   {"id":5,"cmd":"send","thread":"<gid>","type":"group","blocks":[
//        {"type":"image","urls":["https://..."],"caption":"..."},
//        {"type":"text","content":"..."},
//        {"type":"link","url":"https://...","caption":"..."}][, "dryRun":true]}
//   {"id":4,"cmd":"stop"}
//
// Protocol (stdout, NDJSON):
//   {"event":"ready","account":"nick1","ownId":"..."}
//   {"event":"switch","account":"nick2","ok":true[,"error":"..."]}
//   {"event":"msg","account":"nick1","group":"<gid>","idTo":"<gid>","msgId":"...",
//    "uidFrom":"...","name":"...","isSelf":false,"ts":"...","msgType":"webchat",
//    "text":"..."}                      <- a newly seen message
//   {"id":N,"ok":true, ...}             <- reply to a command
//
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { Zalo, ThreadType } from "zca-js";
import { imageSize as probeImageSize } from "image-size";
import { pickMediaUrl, classifyMedia, mediaText, normalizePart, coalesceParts, buildBlocks, sendBlocks, watchFrom, watchGroup, watchState, msgToEvent, WATCH_GROUP_MSG_TYPES } from "./relay.mjs";
import { attachSupervisor } from "./supervise.mjs";

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const k = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) args[k] = true;
      else { args[k] = next; i++; }
    }
  }
  return args;
}

function emit(obj) { process.stdout.write(JSON.stringify(obj) + "\n"); }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function credsPath(dir, account) { return path.join(dir, `${account}.json`); }

function readCreds(dir, account) {
  const p = credsPath(dir, account);
  if (!fs.existsSync(p)) throw new Error(`no credentials for account '${account}' (${p})`);
  return JSON.parse(fs.readFileSync(p, "utf8"));
}

// zca-js needs image dimensions before it can upload a photo; without this
// getter it throws ZaloApiMissingImageMetadataGetter on any attachment send.
async function imageMetadataGetter(filePath) {
  try {
    const buf = await fs.promises.readFile(filePath);
    const dim = probeImageSize(buf);
    if (!dim || !dim.width || !dim.height) return null;
    return { width: dim.width, height: dim.height, size: buf.length };
  } catch {
    return null;
  }
}

function zaloOptions() {
  return { selfListen: true, logging: false, checkUpdate: false, imageMetadataGetter };
}

// The controller is the source of truth for which account this worker should be
// listening as. The initial account lets it start immediately.
const ARGS = parseArgs(process.argv.slice(2));
const CREDS_DIR = ARGS["creds-dir"] || "data/accounts";
// Optional: append every incoming group message (incl. raw `quote`) as NDJSON.
// Lets the panel bot ALSO act as a read-only capture source, so we never open a
// second listener on the same account (which kicks the panel's own socket).
const RAW_DUMP = process.env.ZS_RAW_DUMP || ARGS["raw-dump"] || "";

// Watch allowlist: only these SOURCE group ids (plus an "all" escape hatch)
// produce msg events. Seeded from --watch-groups (controller passes the
// enabled rules' src groups) and refreshable via the `set-watch` command.
// Without it we default to "all" so the worker still works standalone.
const WATCH_ARG = ARGS["watch-groups"];
if (typeof WATCH_ARG === "string" && WATCH_ARG.length) {
  const mode = WATCH_ARG.startsWith("all") ? "all" : "only";
  const ids = (WATCH_ARG.replace(/^all:?/, "") || "").split(",");
  watchFrom(ids, mode);
} else if (WATCH_ARG === true) {
  watchFrom([], "all");
} else {
  watchFrom([], "all");
}

function rawDump(obj) {
  if (!RAW_DUMP) return;
  try { fs.appendFileSync(RAW_DUMP, JSON.stringify(obj) + "\n"); } catch { /* ignore */ }
}

// Diagnostic: dump the ENTIRE raw message object (incl. content) for media
// messages so we can see exactly which keys carry the photo/file URL. Set
// ZS_MEDIA_DUMP=<path> to enable. Bounded by a byte cap to avoid disk blowup.
const MEDIA_DUMP = process.env.ZS_MEDIA_DUMP || "";
let _mediaDumpBytes = 0;
const MEDIA_DUMP_MAX = 2_000_000;
function mediaDump(msg, raw) {
  if (!MEDIA_DUMP) return;
  if (_mediaDumpBytes > MEDIA_DUMP_MAX) return;
  try {
    const rec = { at: Date.now(), msg, raw };
    const s = JSON.stringify(rec);
    _mediaDumpBytes += s.length;
    fs.appendFileSync(MEDIA_DUMP, s + "\n");
  } catch { /* ignore */ }
}

let API = null;
let LISTENER = null;
let CURRENT = null; // account id currently listening
let SUP = null;     // connection supervisor (auto-reconnect on socket death)

// ---- canonical message -> event ----------------------------------------
// Turn one raw zca-js message into the event the controller consumes. Shared by
// the live listener AND the history backfill so a recovered message looks
// EXACTLY like a live one (same dedup key, same part/quote parsing).
// Feed ONE message through the whole pipeline: media diagnostic -> raw dump ->
// stdout event. Returns true when a relayable event was emitted.
function emitCapture(message, account) {
  const t = msgToEvent(message, account);
  if (!t) return false;
  const d = message?.data || {};
  if (/photo|video|image|gif|file|share\./i.test(String(d.msgType || ""))) {
    mediaDump({
      threadId: message?.threadId || "", type: message?.type, isSelf: !!message?.isSelf,
      msgType: d.msgType, keys: Object.keys(d),
      content: d.content, content_type: typeof d.content,
      paramsExt: d.paramsExt ?? null, previewThumb: d.previewThumb ?? null,
    }, d);
  }
  if (RAW_DUMP && t.isGroup) {
    rawDump({
      ev: "msg", at: Date.now(), account, group: t.gid,
      isSelf: !!message?.isSelf, msgId: d.msgId ?? null, realMsgId: d.realMsgId ?? null,
      cliMsgId: d.cliMsgId ?? null, uidFrom: d.uidFrom ?? null, dName: d.dName ?? null,
      ts: d.ts ?? null, msgType: d.msgType ?? null, hasQuote: !!d.quote, quote: t.event.quote,
      partKind: t.event.part.kind, content: t.event.text ?? "", keys: Object.keys(d),
    });
  }
  emit(t.event);
  return true;
}

// ---- history backfill ---------------------------------------------------
// The websocket listener only sees messages while the socket is ALIVE, so any
// post made while it was down (socket death, restart, account offline) was lost
// forever — the root cause of the "miss tin" audit. On every (re)connect we pull
// history over the SAME websocket (zca-js `requestOldMessages`; the REST
// getGroupChatHistory endpoint returns 404 on current Zalo) and replay it through
// the SAME pipeline. The controller's durable `forward_seen` ledger drops anything
// already forwarded, so a replay is safe and idempotent.
//
// Paging semantics (verified live): requestOldMessages returns up to 50 messages
// with ts > cursor (cursor=null -> the 50 newest). So we walk FORWARD from the
// cursor until we reach "now".
let LAST_BACKFILL_MS = 0;
let BACKFILL_INFLIGHT = false;
const BACKFILL_MIN_INTERVAL_MS = 8000;
const OLD_PAGE_LIMIT = 50;
const OLD_REPLAY_MAX_GAP_MS = 8000;   // cap between replayed messages
// On an automatic (re)connect backfill, only relay messages newer than this
// lookback, so a long-dormant rule does not dump days of backlog into the
// destination. A manual/forced backfill (operator/test) ignores it.
const BACKFILL_LOOKBACK_MS = Math.max(1, Number(ARGS["backfill-lookback-hours"]) || 6) * 3600 * 1000;
const _lastSeen = new Map();   // gid -> newest msgId we have replayed this session
let _oldWaiters = [];

function onOldMessages(msgs) {
  const w = _oldWaiters.shift();
  if (w) w(msgs || []);
}

// Ask the server for the NEXT page of group history after `cursor` (null =
// newest page) and resolve with the returned messages (one batch per request).
function requestOldPage(cursor) {
  return new Promise((resolve) => {
    let done = false;
    const fire = (v) => { if (!done) { done = true; resolve(v); } };
    const timer = setTimeout(() => fire([]), 3000);
    if (timer.unref) timer.unref();
    _oldWaiters.push((msgs) => { clearTimeout(timer); fire(msgs); });
    try { LISTENER.requestOldMessages(ThreadType.Group, cursor || null); }
    catch { clearTimeout(timer); fire([]); }
  });
}

// Walk forward from per-group cursors (the controller's durable last_seen_msg_id,
// when supplied) replaying each page until `now` or the page budget is spent.
async function backfillHistory(reason = "connect", force = false, cursors = {}) {
  if (!API || !LISTENER) return { ok: false, error: "chưa đăng nhập" };
  if (BACKFILL_INFLIGHT) return { ok: true, skipped: "đang backfill" };
  const now = Date.now();
  if (!force && now - LAST_BACKFILL_MS < BACKFILL_MIN_INTERVAL_MS) {
    return { ok: true, skipped: "vừa mới backfill" };
  }
  LAST_BACKFILL_MS = now;
  BACKFILL_INFLIGHT = true;
  try {
    return await _backfillInner(reason, force, cursors);
  } finally {
    BACKFILL_INFLIGHT = false;
  }
}

async function _backfillInner(reason, force, cursors) {
  const groups = (watchState().ids || []);
  if (!groups.length) return { ok: true, groups: 0, restored: 0, note: "watch=all, bỏ qua" };
  const maxPages = Math.max(1, Math.min(20, Number(ARGS["backfill-max-pages"]) || 4));
  let restored = 0, scanned = 0;
  const details = [];
  for (const gid of groups) {
    const cursor0 = (cursors && cursors[gid]) ? String(cursors[gid])
                    : (_lastSeen.get(gid) || null);
    // No cursor at all means the rule has never forwarded a live message yet
    // (fresh rule / first run). In that case do NOT relay the backlog — a new
    // rule must start from "now", not spam the destination with old posts. We
    // still page to the live edge so the session cursor is seeded for next time.
    const seedOnly = !cursor0 && !force;
    const paced = !seedOnly;
    const cutMs = force ? 0 : (Date.now() - BACKFILL_LOOKBACK_MS);
    let olderSkipped = 0;
    let cursor = cursor0;
    let pages = 0, gRestored = 0, gScanned = 0, truncated = false, prevTs = 0;
    while (pages < maxPages) {
      let msgs;
      try { msgs = await requestOldPage(cursor); }
      catch (e) { details.push({ group: gid, error: String(e?.message || e) }); break; }
      if (!msgs.length) break;
      pages += 1;
      // keep only this group, oldest -> newest (controller groups image+text)
      const mine = msgs.filter((m) => String(m?.data?.idTo || m?.threadId || "") === String(gid))
                       .sort((a, b) => Number(a?.data?.ts || 0) - Number(b?.data?.ts || 0));
      for (const m of mine) {
        const mts = Number(m?.data?.ts || 0);
        // Auto backfill only relays the recent lookback window; older messages
        // just advance the cursor (manual force restores everything).
        if (!seedOnly && mts && cutMs && mts < cutMs) { olderSkipped += 1; prevTs = mts || prevTs; continue; }
        if (paced) {
          // Replay at the ORIGINAL source tempo: wait out the gap between this
          // message and the previous one (capped) before emitting, so the
          // controller's live coalescing window behaves exactly as it did live.
          if (prevTs && mts > prevTs) {
            const gap = Math.min(mts - prevTs, OLD_REPLAY_MAX_GAP_MS);
            if (gap > 60) await new Promise((r) => setTimeout(r, gap));
          }
          if (mts) prevTs = mts;
        }
        try { if (emitCapture(m, CURRENT)) gRestored += 1; } catch { /* skip bad row */ }
      }
      gScanned += msgs.length;
      const newest = msgs.map((m) => String(m?.data?.msgId || "")).filter(Boolean).pop();
      if (!newest || newest === cursor) break;
      if (newest) _lastSeen.set(gid, newest);
      cursor = newest;
      if (msgs.length < OLD_PAGE_LIMIT) break;   // reached the live edge
      if (pages >= maxPages) { truncated = true; break; }
      await new Promise((r) => setTimeout(r, 350));
    }
    restored += gRestored; scanned += gScanned;
    details.push({ group: gid, scanned: gScanned, replayed: gRestored,
                   pages, from: cursor0 || "(newest)", truncated, seedOnly, olderSkipped });
  }
  emit({ event: "backfill", account: CURRENT, reason, scanned, restored, groups: groups.length, details });
  return { ok: true, reason, scanned, restored, details };
}

async function openAccount(account) {
  if (LISTENER) { try { LISTENER.stop(); } catch {} LISTENER = null; }
  const cred = readCreds(CREDS_DIR, account);
  const zalo = new Zalo(zaloOptions());
  API = await zalo.login({
    imei: cred.imei, cookie: cred.cookie,
    userAgent: cred.userAgent, language: cred.language || "vi",
  });
  const own = (() => { try { return API.getOwnId?.() ?? null; } catch { return null; } })();
  CURRENT = account;

  LISTENER = API.listener;
  LISTENER.on("message", (message) => {
    try {
      const d = message?.data || {};
      const isGroup = message?.type === 1;
      const gid = message?.threadId || d.idTo || "";
      // Watch gate: ignore activity outside the wired-in source groups (text
      // AND media). Keeps the listener quiet and stops unrelated groups from
      // ever being captured/dumped/relayed.
      if (isGroup && !watchGroup(gid)) { return; }
      if (emitCapture(message, account)) SUP?.noteActivity();
    } catch (e) {
      emit({ event: "msg-error", account, error: String(e?.message || e) });
    }
  });
  // History pages requested by backfillHistory (requestOldMessages) land here.
  LISTENER.on("old_messages", (msgs) => onOldMessages(msgs));
  LISTENER.on("error", (e) => emit({ event: "listen-error", account, error: String(e?.message || e) }));
  LISTENER.on("disconnected", (code, reason) => SUP
    ? SUP.onDisconnected(code, reason)
    : emit({ event: "listen-disconnected", account, code, reason: String(reason || "") }));
  LISTENER.on("connected", () => (SUP ? SUP.onConnected() : emit({ event: "listen-connected", account })));
  LISTENER.start({ retryOnClose: true });
  // Backfill history a moment after (re)connect so posts made while the socket
  // was down are replayed (dedup ledger makes this safe/idempotent). Delayed so
  // the `ready` event is emitted first and the panel has its reader attached.
  setTimeout(() => { backfillHistory("connect").catch(() => {}); }, 2500);
  return own;
}

(async () => {
  const initial = ARGS.account;
  if (!initial) { emit({ event: "fatal", message: "--account required" }); process.exit(2); }
  let own = null;
  try {
    own = await openAccount(initial);
  } catch (err) {
    emit({ event: "fatal", message: String(err?.message || err) });
    process.exit(2);
  }
  emit({ event: "ready", account: initial, ownId: own || null, listening: true, watch: watchState() });

  // Self-heal: zca-js will not retry a NORMAL_CLOSURE, so when the socket dies
  // we re-login + re-listen ourselves until told to stop.
  SUP = attachSupervisor({
    emit,
    getAccount: () => CURRENT,
    reconnect: async () => { await openAccount(CURRENT); },
  });

  const rl = readline.createInterface({ input: process.stdin });
  for await (const line of rl) {
    const s = line.trim();
    if (!s) continue;
    let req;
    try { req = JSON.parse(s); } catch { emit({ id: null, ok: false, error: "bad json" }); continue; }
    const { id, cmd } = req;
    try {
      if (cmd === "ping") { emit({ id, ok: true, pong: true, account: CURRENT, watch: watchState() }); continue; }
      if (cmd === "set-watch") {
        const mode = req.mode === "all" ? "all" : "only";
        const ids = Array.isArray(req.groups) ? req.groups : [];
        const s = watchFrom(ids, mode);
        emit({ id, ok: true, watch: s });
        continue;
      }
      if (cmd === "backfill") {
        const r = await backfillHistory("manual", true, req.cursors || {});
        emit({ id, ok: true, ...r });
        continue;
      }
      if (cmd === "switch-to") {
        const target = String(req.account || "").trim();
        if (!target) { emit({ id, ok: false, error: "--account required" }); continue; }
        if (target === CURRENT && LISTENER) { emit({ id, ok: true, account: target, already: true }); continue; }
        const o = await openAccount(target);
        emit({ id, ok: true, account: target, ownId: o || null });
        continue;
      }
      if (cmd === "send") {
        const type = (req.type === "group" || req.type === "1") ? 1 : 0;
        const thread = String(req.thread);
        const replyPrefix = String(req.reply_prefix || "");
        const quote = (req.quote && typeof req.quote === "object") ? req.quote : null;
        let blocks = Array.isArray(req.blocks) ? req.blocks.filter(Boolean) : null;
        if ((!blocks || !blocks.length) && Array.isArray(req.parts) && req.parts.length) {
          blocks = buildBlocks(coalesceParts(req.parts), String(req.prefix || ""), replyPrefix);
        } else if (replyPrefix && blocks && blocks.length) {
          blocks = [{ type: "text", content: replyPrefix }, ...blocks];
        }
        // Echo the resolved blocks so callers can verify how a post maps to
        // Zalo messages (types + captions) — also on the dry-run path.
        const preview = (blocks || []).map((b) => ({
          type: b.type, caption: b.caption || "",
          urls: b.urls ? b.urls.length : (b.url ? 1 : 0), hasExtra: !!b.extra,
        }));
        if (req.dryRun || process.env.ZS_DRY_RUN === "1") {
          emit({ id, ok: true, dryRun: true, thread, type: req.type, preview,
                 reply: replyPrefix || null, quote: quote ? { msgId: quote.msgId, cliMsgId: quote.cliMsgId } : null });
          continue;
        }
        if (blocks && blocks.length) {
          const out = await sendBlocks(API, blocks, thread, type, CURRENT,
            (err) => emit({ event: "media-error", account: CURRENT, error: err }), quote);
          emit({ id, ok: true, msgId: out.msgId, msgIds: out.msgIds, images: out.images, preview });
          continue;
        }
        const msg = String(req.text ?? "");
        const res = await API.sendMessage(quote ? { msg, quote } : { msg }, thread, type);
        const mid = res?.message?.msgId ?? res?.msgId ?? res?.attachment?.[0]?.msgId ?? "";
        emit({ id, ok: true, msgId: String(mid || "") });
        continue;
      }
      if (cmd === "stop") { SUP?.stop(); emit({ id, ok: true }); break; }
      emit({ id, ok: false, error: `unknown cmd ${cmd}` });
    } catch (err) {
      emit({ id, ok: false, error: String(err?.message || err) });
    }
  }
  try { SUP?.stop(); LISTENER?.stop(); } catch {}
  process.exit(0);
})();
