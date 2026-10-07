#!/usr/bin/env node
/**
 * sessiond.mjs — SINGLE Zalo session owner per account.
 *
 * Why: Zalo allows only one live session per account. When the campaign sender
 * (a fresh `zalo.mjs send` login each call) and the G2G listener (`autobot.mjs`)
 * both logged into the SAME account, each new login KICKED the other off the
 * socket — campaign sends failed with "fetch failed" and the G2G listener went
 * deaf. This daemon logs in ONCE and serves every client over a unix socket, so
 * there is never more than one session per account.
 *
 * It is exactly `zalo.mjs daemon` + `autobot.mjs` merged onto one session/API:
 *   login once → { one-shot commands, send, listener events, watch, backfill }
 *
 * Usage:
 *   node sessiond.mjs --account nick1 --sock <path> [--creds-dir D] \
 *        [--watch-groups only:g1,g2|all] [--backfill-lookback-hours 6] [--idle-exit-s 0]
 *
 * Protocol: newline-delimited JSON over a unix socket (server → client). This
 * mirrors the message shapes the Python controller already consumes from
 * autobot.mjs (`event:"msg"`, `{id,ok,...}` replies), plus one-shot replies.
 */
import fs from "node:fs";
import os from "node:os";
import net from "node:net";
import path from "node:path";
import { Zalo, ThreadType } from "zca-js";
import { imageSize as probeImageSize } from "image-size";
import { doSend, doFindUsers, doUserInfo, doFriendRequest, doSetAlias, doGroups, doGroupMembers, loginApi } from "./zalo.mjs";
import { normalizePart, watchFrom, watchGroup, watchState, msgToEvent, coalesceParts, buildBlocks, sendBlocks } from "./relay.mjs";

const zaloOptions = () => ({ logging: false, imageMetadataGetter: async (fp) => {
  try { const b = await fs.promises.readFile(fp); const d = probeImageSize(b);
    return d && d.width && d.height ? { width: d.width, height: d.height, size: b.length } : null;
  } catch { return null; }
} });

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

const ARGS = parseArgs(process.argv.slice(2));
const ACCOUNT = ARGS.account || "default";
const SOCK = ARGS.sock || path.join(os.tmpdir(), `zs-sessiond-${ACCOUNT}.sock`);
const CREDS_DIR = ARGS["creds-dir"] || process.env.ZS_CREDS_DIR || path.join(process.cwd(), "data", "accounts");
const IDLE_EXIT_MS = Number(ARGS["idle-exit-s"] || 0) * 1000 || 0;
const OLD_PAGE_LIMIT = 50;
const OLD_REPLAY_MAX_GAP_MS = 8000;
const BACKFILL_LOOKBACK_MS = Math.max(1, Number(ARGS["backfill-lookback-hours"]) || 6) * 3600 * 1000;
const HEARTBEAT_MS = 45000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const logErr = (...m) => { try { process.stderr.write(`[sessiond:${ACCOUNT}] ${m.join(" ")}\n`); } catch {} };

// ---------------------------------------------------------------- clients

const clients = new Set();
function broadcast(obj) {
  const line = JSON.stringify(obj) + "\n";
  for (const s of clients) { try { s.write(line); } catch { /* dropped */ } }
}

// ---------------------------------------------------------------- state

let API = null;
let LISTENER = null;
let ownId = "";
let lastActivity = Date.now();
let ready = false;
let loginInfo = null;

const watch = watchFrom([], "all");
function applyWatchArg(spec) {
  // spec: "all" | "only:g1,g2"
  const s = String(spec || "all");
  if (s.startsWith("only:")) return watchFrom(s.slice(5).split(",").map((x) => x.trim()).filter(Boolean), "only");
  return watchFrom([], "all");
}

// ---------------------------------------------------------------- login

async function doLogin() {
  const cred = JSON.parse(fs.readFileSync(path.join(CREDS_DIR, `${ACCOUNT}.json`), "utf8"));
  const zalo = new Zalo(zaloOptions());
  API = await zalo.login({ imei: cred.imei, cookie: cred.cookie, userAgent: cred.userAgent, language: cred.language || "vi" });
  ownId = (() => { try { return String(API.getOwnId?.() ?? ""); } catch { return ""; } })();
  LISTENER = API.listener;
  wireListener();
  LISTENER.start({ retryOnClose: true });
  ready = true;
  broadcast({ event: "ready", account: ACCOUNT, ownId, socket_alive: true });
  logErr("ready", ownId || "(no ownId)");
  // Auto-backfill shortly after connect so posts missed while down are replayed.
  setTimeout(() => { backfill("connect").catch(() => {}); }, 2500);
}

function wireListener() {
  LISTENER.on("message", (message) => {
    try {
      const d = message?.data || {};
      const isGroup = message?.type === 1;
      const gid = message?.threadId || d.idTo || "";
      if (isGroup && !watchGroup(gid)) return;
      const t = msgToEvent(message, ACCOUNT);
      if (!t) return;
      lastActivity = Date.now();
      broadcast(t.event);
    } catch (e) {
      broadcast({ event: "msg-error", account: ACCOUNT, error: String(e?.message || e) });
    }
  });
  LISTENER.on("old_messages", (msgs) => onOldMessages(msgs));
}

// ---------------------------------------------------------------- history/backfill

let oldWaiters = [];
function onOldMessages(msgs) { const w = oldWaiters.shift(); if (w) w(msgs || []); }
function requestOldPage(cursor) {
  return new Promise((resolve) => {
    let done = false;
    const fire = (v) => { if (!done) { done = true; resolve(v); } };
    const timer = setTimeout(() => fire([]), 3000); if (timer.unref) timer.unref();
    oldWaiters.push((msgs) => { clearTimeout(timer); fire(msgs); });
    try { LISTENER.requestOldMessages(ThreadType.Group, cursor || null); }
    catch { clearTimeout(timer); fire([]); }
  });
}
let backfilling = false;
async function backfill(reason, force = false, cursors = {}) {
  if (!LISTENER) return { ok: false, error: "chưa đăng nhập" };
  if (backfilling) return { ok: true, skipped: "đang backfill" };
  backfilling = true;
  try { return await backfillInner(reason, force, cursors); }
  finally { backfilling = false; }
}
async function backfillInner(reason, force, cursors) {
  const groups = (watchState().ids || []);
  if (!groups.length) return { ok: true, groups: 0, restored: 0, note: "watch=all" };
  const maxPages = Math.max(1, Math.min(20, Number(ARGS["backfill-max-pages"]) || 4));
  const cutMs = force ? 0 : (Date.now() - BACKFILL_LOOKBACK_MS);
  let restored = 0, scanned = 0; const details = [];
  for (const gid of groups) {
    const cursor0 = (cursors && cursors[gid]) ? String(cursors[gid]) : null;
    const seedOnly = !cursor0 && !force;
    let cursor = cursor0, pages = 0, gRestored = 0, gScanned = 0, prevTs = 0, olderSkipped = 0;
    while (pages < maxPages) {
      const msgs = await requestOldPage(cursor);
      if (!msgs.length) break;
      pages += 1;
      const mine = msgs.filter((m) => String(m?.data?.idTo || m?.threadId || "") === String(gid))
                       .sort((a, b) => Number(a?.data?.ts || 0) - Number(b?.data?.ts || 0));
      for (const m of mine) {
        const mts = Number(m?.data?.ts || 0);
        if (seedOnly || (mts && cutMs && mts < cutMs)) { if (mts) prevTs = mts; olderSkipped += 1; continue; }
        if (prevTs && mts > prevTs) { const gap = Math.min(mts - prevTs, OLD_REPLAY_MAX_GAP_MS); if (gap > 60) await sleep(gap); }
        if (mts) prevTs = mts;
        try { const t = msgToEvent(m, ACCOUNT); if (t) { broadcast(t.event); gRestored += 1; } } catch { /* skip */ }
      }
      gScanned += msgs.length;
      const newest = msgs.map((m) => String(m?.data?.msgId || "")).filter(Boolean).pop();
      if (!newest || newest === cursor) break;
      cursor = newest;
      if (msgs.length < OLD_PAGE_LIMIT) break;
      if (pages >= maxPages) break;
      await sleep(350);
    }
    restored += gRestored; scanned += gScanned;
    details.push({ group: gid, scanned: gScanned, replayed: gRestored, pages, seedOnly, olderSkipped });
  }
  broadcast({ event: "backfill", account: ACCOUNT, reason, scanned, restored, groups: groups.length, details });
  logErr("backfill", reason, "scanned", scanned, "restored", restored);
  return { ok: true, reason, scanned, restored, details };
}

// ------------------------------------------------------------------ commands

async function handle(req) {
  const { id, cmd } = req;
  switch (cmd) {
    case "ping": return { id, ok: true, pong: true };
    case "status": return { id, ok: true, account: ACCOUNT, ownId, ready, socket_alive: true, watch: watchState(), last_activity: lastActivity };
    case "whoami": { const info = await API.fetchAccountInfo(); return { id, ok: true, profile: info?.profile ?? info }; }
    case "send": {
      if (req.dryRun) return { id, ok: true, dryRun: true, thread: req.thread, type: req.type };
      const quote = (req.quote && typeof req.quote === "object") ? req.quote : null;
      const rp = String(req.reply_prefix || "");
      // (1) Explicit ordered blocks.
      let blocks = Array.isArray(req.blocks) ? req.blocks.filter(Boolean) : null;
      // (2) Canonical parts (G2G forward): coalesce + build ordered blocks.
      if ((!blocks || !blocks.length) && Array.isArray(req.parts) && req.parts.length) {
        blocks = buildBlocks(coalesceParts(req.parts), String(req.prefix || ""), rp);
      } else if (rp && blocks && blocks.length) {
        blocks = [{ type: "text", content: rp }, ...blocks];
      }
      if (blocks && blocks.length) {
        const out = await sendBlocks(API, blocks, req.thread, req.type, ACCOUNT,
          (err) => broadcast({ event: "media-error", account: ACCOUNT, error: err }), quote);
        return { id, ok: true, msgId: out.msgId, msgIds: out.msgIds, images: out.images };
      }
      // (3) Legacy one-shot (campaign): url / images album / plain text.
      return { id, ok: true, ...(await doSend(API, req)) };
    }
    case "find-users": return { id, ok: true, users: await doFindUsers(API, req.phones || []) };
    case "user-info": return { id, ok: true, users: await doUserInfo(API, req.userIds || req.users || []) };
    case "friend-request":
      if (req.dryRun) return { id, ok: true, dryRun: true, userId: req.userId ?? req.user };
      return { id, ok: true, ...(await doFriendRequest(API, req.userId ?? req.user, req.message)) };
    case "set-alias":
      if (req.dryRun) return { id, ok: true, dryRun: true };
      return { id, ok: true, ...(await doSetAlias(API, req.userId ?? req.user, req.alias)) };
    case "groups": return { id, ok: true, groups: await doGroups(API) };
    case "group-members": {
      // Stream progress as events tagged with this request id, then reply with
      // the final member list. The client filters events by req_id.
      const onProgress = req.stream ? (p) => broadcast({ event: "scan", req_id: id, ...p }) : null;
      const r = await doGroupMembers(API, req.groupId ?? req.group, onProgress);
      return { id, ok: true, ...r };
    }
    case "set-watch": { const s = applyWatchArg(req.mode === "only" ? "only:" + (req.groups || []).join(",") : "all"); return { id, ok: true, watch: s }; }
    case "backfill": return { id, ok: true, ...(await backfill("manual", true, req.cursors || {})) };
    case "stop":
      broadcast({ id, ok: true, stopping: true });
      setTimeout(() => shutdown(0), 50);
      return null; // already replied
    default: return { id, ok: false, error: `unknown cmd ${cmd}` };
  }
}

// ------------------------------------------------------------------ server

function shutdown(code = 0) {
  try { LISTENER?.stop?.(); } catch {}
  for (const s of clients) { try { s.end(); } catch {} }
  try { fs.unlinkSync(SOCK); } catch {}
  process.exit(code);
}

function startServer() {
  try { fs.unlinkSync(SOCK); } catch {}
  const server = net.createServer((sock) => {
    clients.add(sock);
    sock.setNoDelay(true);
    lastActivity = Date.now();
    // On attach, immediately tell the client the current state. If we are
    // already ready, re-send a `ready` event too, so a client that attaches
    // AFTER the daemon booted still learns the session is up (otherwise it
    // would sit forever with ready=false while able to send).
    try {
      sock.write(JSON.stringify({ event: "hello", account: ACCOUNT, ready, ownId, watch: watchState() }) + "\n");
      if (ready) sock.write(JSON.stringify({ event: "ready", account: ACCOUNT, ownId, watch: watchState() }) + "\n");
    } catch {}
    let buf = "";
    sock.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim(); buf = buf.slice(idx + 1);
        if (!line) continue;
        lastActivity = Date.now();
        let req; try { req = JSON.parse(line); } catch { continue; }
        (async () => {
          try {
            const rep = await handle(req);
            if (rep) { try { sock.write(JSON.stringify(rep) + "\n"); } catch {} }
          } catch (e) {
            try { sock.write(JSON.stringify({ id: req?.id ?? null, ok: false, error: String(e?.message || e) }) + "\n"); } catch {}
          }
        })();
      }
    });
    sock.on("error", () => {});
    sock.on("close", () => { clients.delete(sock); });
  });
  server.on("error", (e) => { logErr("server error", String(e?.message || e)); shutdown(2); });
  server.listen(SOCK, () => { try { fs.chmodSync(SOCK, 0o600); } catch {} logErr("listening", SOCK); });
  return server;
}

// ---------------------------------------------------------------- heartbeat/idle

function startHeartbeat() {
  const hb = setInterval(() => { if (ready) broadcast({ event: "heartbeat", account: ACCOUNT, connected: true, lastActivity }); }, HEARTBEAT_MS);
  if (hb.unref) hb.unref();
}
function startIdleWatch(server) {
  if (!IDLE_EXIT_MS) return;
  const t = setInterval(() => {
    if (Date.now() - lastActivity > IDLE_EXIT_MS) { logErr("idle exit"); shutdown(0); }
  }, Math.min(IDLE_EXIT_MS, 30000));
  if (t.unref) t.unref();
}

async function main() {
  const server = startServer();
  try { await doLogin(); }
  catch (e) {
    broadcast({ event: "fatal", account: ACCOUNT, error: String(e?.message || e) });
    logErr("login failed", String(e?.message || e));
    shutdown(2); return;
  }
  startHeartbeat();
  startIdleWatch(server);
  process.on("SIGTERM", () => shutdown(0));
  process.on("SIGINT", () => shutdown(0));
}

main().catch((e) => { logErr("fatal", String(e?.stack || e)); process.exit(2); });
