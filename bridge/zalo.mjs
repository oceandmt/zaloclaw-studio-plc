#!/usr/bin/env node
/**
 * zaloclaw-studio bridge (zca-js).
 *
 * Commands:
 *   login-qr  --account <id> [--creds-dir D] [--emit F]   QR login; writes ndjson events to --emit
 *   daemon    --account <id> [--creds-dir D]              persistent ndjson stdin/stdout worker
 *   whoami    --account <id> [--creds-dir D]              print account info as JSON
 *   send      --account <id> --thread <id> --type user|group --text "..."
 *                [--url <link>] [--images a.jpg,b.png] [--file-caption "..."]
 *                [--dry-run] [--json]
 *             (--images sends a MULTI-PHOTO album; --file-caption is its caption;
 *              --text is sent as a separate message after the photos)
 *   find-users --account <id> --phones 09xx,09yy [--json]
 *   groups     --account <id> [--json]                 list groups the account is in
 *   group-members --account <id> --group <groupId> [--json]   scrape members of a group
 *   friend-request --account <id> --user <id> [--message "..."] [--dry-run] [--json]
 *   set-alias --account <id> --user <id> --alias "Tên KH" [--dry-run] [--json]
 *
 * Safety: --dry-run (or env ZS_DRY_RUN=1) NEVER constructs a live API call for
 * side-effecting commands; it returns a simulated result. Default for send/
 * friend-request is LIVE unless --dry-run is passed, so callers must choose.
 *
 * Exit: 0 ok, 1 partial failure, 2 fatal.
 */

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import readline from "node:readline";
import { Zalo, ThreadType, LoginQRCallbackEventType } from "zca-js";
import { imageSize as probeImageSize } from "image-size";

// zca-js needs image dimensions before it can upload a photo. We probe them
// locally (no native deps) so callers can pass plain file paths.
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
const zaloOptions = () => ({ logging: false, imageMetadataGetter });

// ---------------------------------------------------------------- args/utils

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) out[key] = true;
      else { out[key] = next; i++; }
    } else out._.push(a);
  }
  return out;
}

const DRY = (args) => args["dry-run"] === true || process.env.ZS_DRY_RUN === "1";
const LOG = (...m) => { if (!process.argv.includes("--json")) console.error(...m); };

function credsDir(args) {
  return args["creds-dir"] || process.env.ZS_CREDS_DIR || path.join(process.cwd(), "data", "accounts");
}
function credsPath(args) {
  const id = args.account || "default";
  return path.join(credsDir(args), `${id}.json`);
}
function readCreds(args) {
  const p = credsPath(args);
  if (!fs.existsSync(p)) throw new Error(`no credentials for account '${args.account || "default"}' (${p})`);
  return JSON.parse(fs.readFileSync(p, "utf8"));
}

// Normalise a comma / newline separated list of image paths.
function splitList(v) {
  if (Array.isArray(v)) return v.flatMap((x) => splitList(x));
  return String(v ?? "").split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
}
function writeCreds(args, data) {
  const dir = credsDir(args);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const p = credsPath(args);
  fs.writeFileSync(p, JSON.stringify(data, null, 2), { mode: 0o600 });
  return p;
}
function emitJson(obj) { process.stdout.write(JSON.stringify(obj) + "\n"); }

// ------------------------------------------------------------------- loginQR

async function cmdLoginQr(args) {
  const emitPath = args.emit;
  const emit = (obj) => {
    const line = JSON.stringify({ ts: Date.now(), ...obj });
    if (emitPath) fs.appendFileSync(emitPath, line + "\n");
    emitJson(obj);
  };
  const zalo = new Zalo(zaloOptions());
  let retries = 0;
  const MAX_RETRY = 20;
  try {
    const api = await zalo.loginQR({ userAgent: args["user-agent"] }, (event) => {
      switch (event.type) {
        case LoginQRCallbackEventType.QRCodeGenerated:
          emit({ event: "qr", image: event.data.image, code: event.data.code });
          break;
        case LoginQRCallbackEventType.QRCodeExpired:
          emit({ event: "expired", retry: retries + 1 });
          // auto-regenerate a fresh QR so the user always sees a valid code
          if (retries < MAX_RETRY) {
            retries++;
            try { event.actions.retry(); } catch (e) { emit({ event: "error", message: "retry failed: " + e }); }
          } else {
            emit({ event: "error", message: "QR hết hạn quá nhiều lần, bấm Tạo mã mới" });
          }
          break;
        case LoginQRCallbackEventType.QRCodeScanned:
          emit({ event: "scanned", name: event.data.display_name, avatar: event.data.avatar });
          break;
        case LoginQRCallbackEventType.QRCodeDeclined:
          emit({ event: "declined" });
          break;
        case LoginQRCallbackEventType.GotLoginInfo:
          // session captured; persist below via returned cookies
          break;
        default:
          break;
      }
    });
    if (!api) { emit({ event: "error", message: "login returned null" }); process.exit(1); }
    const info = await api.fetchAccountInfo().catch(() => null);
    const profile = info?.profile ?? info ?? {};
    // Persist credentials from the live context.
    const ctx = api.getContext?.() ?? api.context ?? null;
    const creds = {
      imei: ctx?.imei ?? null,
      cookie: ctx?.cookie?.toJSON?.() ?? ctx?.cookie ?? null,
      userAgent: ctx?.userAgent ?? args["user-agent"] ?? null,
      language: "vi",
      userId: profile?.userId ?? null,
      displayName: profile?.displayName ?? profile?.zaloName ?? null,
    };
    const p = writeCreds(args, creds);
    emit({ event: "done", creds: p, userId: creds.userId, name: creds.displayName });
    process.exit(0);
  } catch (err) {
    emit({ event: "error", message: String(err?.message || err) });
    process.exit(2);
  }
}

// ------------------------------------------------------------- session login

async function loginApi(args) {
  const cred = readCreds(args);
  const zalo = new Zalo(zaloOptions());
  const api = await zalo.login({
    imei: cred.imei,
    cookie: cred.cookie,
    userAgent: cred.userAgent,
    language: cred.language || "vi",
  });
  return api;
}
const ttype = (t) => (String(t).toLowerCase() === "group" ? ThreadType.Group : ThreadType.User);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function chunks(arr, n) { const o = []; for (let i = 0; i < arr.length; i += n) o.push(arr.slice(i, i + n)); return o; }

// ------------------------------------------------------------------ actions

function msgIdOf(res) {
  if (!res) return "";
  if (typeof res === "number") return String(res);
  return String(res?.msgId ?? res?.message?.msgId ?? res?.message?.data?.msgId ?? "");
}

// One send call may deliver: an album of photos (with optional caption) + a
// plain text message. zca-js sends msg separately from multi-attachments, so we
// issue up to two network calls and return both msgIds.
// Normalize message line endings. Browsers post CRLF (\r\n); Zalo renders \r
// AND \n each as a line break, so CRLF shows as a blank line between every
// line (double-spaced). Collapse to a single \n, and strip per-line trailing
// spaces + Unicode separators.
function normText(s) {
  if (s == null) return "";
  return String(s)
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .replace(/\u2028|\u2029|\u0085/g, "\n")
    .split("\n")
    .map((ln) => ln.replace(/[ \t]+$/g, ""))
    .join("\n")
    .replace(/^\n+|\n+$/g, "");
}

function normBlocks(v) {
  if (!v) return null;
  let arr = v;
  if (typeof v === "string") {
    try { arr = JSON.parse(v); } catch { return null; }
  }
  return Array.isArray(arr) ? arr : null;
}

export async function doSend(api, { thread, type, text, url, caption, images, fileText, blocks }) {
  const tt = ttype(type);

  // Preferred: an explicit ORDERED block list. Each block is sent in sequence
  // so text/photos can be interleaved and a photo can carry its own caption.
  // Shape: [{type:'text',content}, {type:'image',src|caption}, {type:'image',images[],caption}, {type:'link',url,caption}]
  const bl = normBlocks(blocks);
  if (bl && bl.length) {
    const ids = [];
    for (const b of bl) {
      if (!b || typeof b !== "object") continue;
      const bt = String(b.type || "").toLowerCase();
      if (bt === "text") {
        const msg = normText(b.content);
        if (!msg) continue;
        const res = await api.sendMessage({ msg }, thread, tt);
        const mid = msgIdOf(res?.message) || msgIdOf(res);
        if (mid) ids.push(mid);
      } else if (bt === "image") {
        const imgs = (Array.isArray(b.images) ? b.images : [b.src]).filter(Boolean);
        if (!imgs.length) continue;
        const cap = normText(b.caption ?? "");
        const res = await api.sendMessage({ msg: cap, attachments: imgs }, thread, tt);
        const mid = msgIdOf(res?.attachment?.[0]) || msgIdOf(res?.message) || msgIdOf(res);
        if (mid) ids.push(mid);
      } else if (bt === "link") {
        const u = String(b.url || "").trim();
        if (!u) continue;
        const cap = normText(b.caption ?? "");
        const res = await api.sendLink({ link: u, ...(cap ? { msg: cap } : {}) }, thread, tt);
        const mid = msgIdOf(res);
        if (mid) ids.push(mid);
      }
    }
    if (!ids.length) return { ok: false, error: "empty message" };
    return { ok: true, msgId: ids[ids.length - 1], msgIds: ids, blocks: bl.length };
  }

  const imgs = Array.isArray(images) ? images.filter(Boolean) : splitList(images);
  const albumCaption = normText(caption ?? fileText ?? "");
  const plainText = normText(text);

  let msgId = "";
  let albumMsgId = "";
  let textMsgId = "";

  // Link card (mutually exclusive with a photo album in this UI).
  if (url && imgs.length === 0) {
    const res = await api.sendLink({ link: url, ...(albumCaption || plainText ? { msg: albumCaption || plainText } : {}) }, thread, tt);
    return { ok: true, msgId: msgIdOf(res) };
  }

  // Photo album (multi-image). If a caption is set it is sent as the album
  // description; the plain text (if any) follows as its own message below.
  if (imgs.length > 0) {
    const res = await api.sendMessage(
      { msg: albumCaption, attachments: imgs },
      thread, tt,
    );
    albumMsgId = msgIdOf(res?.attachment?.[0]) || msgIdOf(res?.message);
    msgId = albumMsgId;
  }

  // Plain text (either the only content, or the text that follows the album).
  if (plainText) {
    const res = await api.sendMessage({ msg: plainText }, thread, tt);
    textMsgId = msgIdOf(res?.message);
    msgId = textMsgId || msgId;
  }

  if (!msgId && imgs.length === 0 && !plainText) {
    // nothing to send — treat as empty message rather than silent success
    return { ok: false, error: "empty message" };
  }
  return { ok: true, msgId, albumMsgId, textMsgId, images: imgs.length };
}

function normPhone(p) {
  const d = String(p || "").replace(/\D/g, "");
  if (!d) return "";
  if (d.startsWith("84")) return d;
  if (d.startsWith("0")) return "84" + d.slice(1);
  return d;
}

function genderStr(g) {
  if (typeof g === "number") return g === 0 ? "nam" : g === 1 ? "nữ" : "";
  const s = String(g || "").trim().toLowerCase();
  if (["m", "nam", "male"].includes(s)) return "nam";
  if (["f", "nữ", "nu", "female"].includes(s)) return "nữ";
  return "";
}

async function doFindUsers(api, phones) {
  const res = await api.getMultiUsersByPhones(phones);
  // NOTE: getMultiUsersByPhones returns a map keyed by NORMALIZED PHONE (e.g.
  // "84367847099"), value = { uid, display_name, gender, ... }. It is NOT nested
  // under changed_profiles (that shape is for getUserInfo by userId).
  const map = res?.changed_profiles || res?.changedProfiles || res?.data || res || {};
  const wanted = {};
  for (const ph of phones) wanted[normPhone(ph)] = ph;
  const out = [];
  for (const [key, p] of Object.entries(map)) {
    if (!p || typeof p !== "object") continue;
    const uid = p.uid || p.userId;
    if (!uid) continue;
    out.push({
      userId: String(uid),
      phone: wanted[normPhone(key)] || p.phoneNumber || key,
      displayName: p.displayName || p.display_name || p.zaloName || p.zalo_name || "",
      gender: genderStr(p.gender),
      avatar: p.avatar || "",
    });
  }
  return out;
}

async function doUserInfo(api, userIds) {
  // Look profiles up by ZALO USER ID (not phone). Group-derived contacts store
  // their userId in the `phone` column, so this is how we fill gender for them.
  // getUserInfo accepts a batch and returns changed_profiles keyed by uid.
  const ids = (Array.isArray(userIds) ? userIds : [userIds]).map((s) => String(s).trim()).filter(Boolean);
  if (ids.length === 0) return [];
  const out = [];
  for (const chunk of chunks(ids, 40)) {
    let res;
    try { res = await api.getUserInfo(chunk); } catch { continue; }
    const map = res?.changed_profiles || res?.changedProfiles || {};
    for (const uid of chunk) {
      const p = map[uid] || map[String(uid)];
      if (!p) continue;
      out.push({
        userId: String(p.userId || uid),
        phone: p.phoneNumber || "",
        displayName: p.displayName || p.zaloName || p.zalo_name || "",
        gender: genderStr(p.gender),
        avatar: p.avatar || "",
      });
    }
    await sleep(250);
  }
  return out;
}

async function doFriendRequest(api, userId, message) {
  await api.sendFriendRequest(message || "Xin chào, mình muốn kết bạn!", userId);
  return { ok: true };
}

async function doSetAlias(api, userId, alias) {
  if (!alias || !String(alias).trim()) return { ok: false, error: "empty alias" };
  await api.changeFriendAlias(String(alias).trim(), userId);
  return { ok: true, userId, alias: String(alias).trim() };
}

async function doGroups(api) {
  const all = await api.getAllGroups();
  const ids = Object.keys(all?.gridVerMap || {});
  const out = [];
  for (const chunk of chunks(ids, 30)) {
    let info;
    try { info = await api.getGroupInfo(chunk); } catch { continue; }
    const map = info?.gridInfoMap || {};
    for (const [gid, g] of Object.entries(map)) {
      const memIds = g?.memberIds || (g?.currentMems || []).map((m) => m?.id).filter(Boolean);
      out.push({
        groupId: gid,
        name: g?.name || "",
        totalMember: g?.totalMember ?? memIds.length,
        memberCount: memIds.length,
        adminCount: (g?.adminIds || []).length,
        avt: g?.avt || "",
        type: g?.type ?? null,
      });
    }
    await sleep(300);
  }
  out.sort((a, b) => (b.totalMember || 0) - (a.totalMember || 0));
  return out;
}

async function doGroupMembers(api, groupId, onProgress) {
  const info = await api.getGroupInfo([groupId]);
  const map = info?.gridInfoMap || {};
  const g = map[groupId] || Object.values(map)[0] || {};
  // memberIds may be empty on modern Zalo; fall back to memVerList ("uid_version")
  let ids = g?.memberIds || (g?.currentMems || []).map((m) => m?.id).filter(Boolean);
  if (!ids || ids.length === 0) {
    ids = (g?.memVerList || []).map((e) => String(e).split("_")[0]).filter(Boolean);
  }
  ids = [...new Set(ids)];
  const adminIds = new Set(g?.adminIds || []);
  const total = g?.totalMember || ids.length;
  const prog = typeof onProgress === "function" ? onProgress : () => {};
  prog({ phase: "start", name: g?.name || "", total, fetched: 0, chunks: chunks(ids, 50).length });
  const profiles = {};
  let done = 0;
  for (const chunk of chunks(ids, 50)) {
    try {
      const r = await api.getGroupMembersInfo(chunk);
      Object.assign(profiles, r?.profiles || {});
    } catch { /* skip chunk */ }
    done += 1;
    prog({ phase: "chunk", total, fetched: Object.keys(profiles).length, chunk: done,
           chunks: chunks(ids, 50).length });
    await sleep(400);
  }
  const members = Object.entries(profiles).map(([uid, p]) => ({
    userId: uid,
    displayName: p?.displayName || p?.zaloName || "",
    zaloName: p?.zaloName || "",
    avatar: p?.avatar || "",
    gender: genderStr(p?.gender),
    globalId: p?.globalId || "",
    isAdmin: adminIds.has(uid),
    accountStatus: p?.accountStatus ?? null,
  }));
  return {
    groupId,
    name: g?.name || "",
    totalMember: total,
    returned: members.length,
    partial: members.length < total,
    members,
  };
}

// -------------------------------------------------------------------- daemon

async function cmdDaemon(args) {
  let api;
  try {
    api = await loginApi(args);
  } catch (err) {
    emitJson({ event: "fatal", message: String(err?.message || err) });
    process.exit(2);
  }
  const own = (() => { try { return api.getOwnId?.() ?? null; } catch { return null; } })();
  emitJson({ event: "ready", account: args.account || "default", ownId: own });

  const rl = readline.createInterface({ input: process.stdin });
  for await (const line of rl) {
    const s = line.trim();
    if (!s) continue;
    let req;
    try { req = JSON.parse(s); } catch { emitJson({ id: null, ok: false, error: "bad json" }); continue; }
    const { id, cmd } = req;
    try {
      if (cmd === "ping") { emitJson({ id, ok: true, pong: true }); continue; }
      if (cmd === "whoami") {
        const info = await api.fetchAccountInfo();
        emitJson({ id, ok: true, profile: info?.profile ?? info });
        continue;
      }
      if (cmd === "send") {
        if (req.dryRun || process.env.ZS_DRY_RUN === "1") {
          const nb = normBlocks(req.blocks);
          emitJson({ id, ok: true, dryRun: true, thread: req.thread, type: req.type, blocks: nb ? nb.length : 0 });
          continue;
        }
        const r = await doSend(api, req);
        emitJson({ id, ok: r.ok !== false, ...r });
        continue;
      }
      if (cmd === "find-users") {
        const r = await doFindUsers(api, req.phones || []);
        emitJson({ id, ok: true, users: r });
        continue;
      }
      if (cmd === "user-info") {
        const r = await doUserInfo(api, req.userIds || req.users || []);
        emitJson({ id, ok: true, users: r });
        continue;
      }
      if (cmd === "friend-request") {
        if (req.dryRun || process.env.ZS_DRY_RUN === "1") {
          emitJson({ id, ok: true, dryRun: true, userId: req.userId });
          continue;
        }
        const r = await doFriendRequest(api, req.userId, req.message);
        emitJson({ id, ok: true, ...r });
        continue;
      }
      if (cmd === "groups") {
        emitJson({ id, ok: true, groups: await doGroups(api) });
        continue;
      }
      if (cmd === "group-members") {
        const r = await doGroupMembers(api, req.groupId);
        emitJson({ id, ok: true, ...r });
        continue;
      }
      emitJson({ id, ok: false, error: `unknown cmd ${cmd}` });
    } catch (err) {
      emitJson({ id, ok: false, error: String(err?.message || err) });
    }
  }
  process.exit(0);
}

// -------------------------------------------------------------- one-shot cmds

async function cmdWhoami(args) {
  const api = await loginApi(args);
  const info = await api.fetchAccountInfo();
  emitJson({ ok: true, profile: info?.profile ?? info });
}

async function cmdSend(args) {
  const images = splitList(args.images);
  const blocks = normBlocks(args.blocks);
  if (DRY(args)) { emitJson({ ok: true, dryRun: true, thread: args.thread, images, blocks: blocks ? blocks.length : 0 }); return; }
  const api = await loginApi(args);
  const r = await doSend(api, {
    thread: args.thread, type: args.type, text: args.text, url: args.url,
    caption: args.caption, images, blocks,
    fileText: args["file-caption"] === true ? "" : args["file-caption"],
  });
  emitJson({ ok: r.ok !== false, ...r });
}

async function cmdFindUsers(args) {
  const api = await loginApi(args);
  const phones = String(args.phones || "").split(",").map((s) => s.trim()).filter(Boolean);
  emitJson({ ok: true, users: await doFindUsers(api, phones) });
}

async function cmdUserInfo(args) {
  const api = await loginApi(args);
  const ids = String(args["user-ids"] || args.users || "").split(",").map((s) => s.trim()).filter(Boolean);
  emitJson({ ok: true, users: await doUserInfo(api, ids) });
}

async function cmdFriendRequest(args) {
  if (DRY(args)) { emitJson({ ok: true, dryRun: true, userId: args.user }); return; }
  const api = await loginApi(args);
  emitJson({ ok: true, ...(await doFriendRequest(api, args.user, args.message)) });
}

async function cmdSetAlias(args) {
  if (!args.user) { emitJson({ ok: false, error: "--user is required" }); process.exit(2); }
  if (DRY(args)) { emitJson({ ok: true, dryRun: true, userId: args.user, alias: args.alias }); return; }
  const api = await loginApi(args);
  emitJson({ ok: true, ...(await doSetAlias(api, args.user, args.alias)) });
}

async function cmdGroups(args) {
  const api = await loginApi(args);
  emitJson({ ok: true, groups: await doGroups(api) });
}

async function cmdGroupMembers(args) {
  if (!args.group) { emitJson({ ok: false, error: "--group is required" }); process.exit(2); }
  const api = await loginApi(args);
  emitJson({ ok: true, ...(await doGroupMembers(api, args.group)) });
}

async function cmdGroupMembersStream(args) {
  if (!args.group) { emitJson({ ok: false, error: "--group is required" }); process.exit(2); }
  let api;
  try {
    api = await loginApi(args);
  } catch (err) {
    emitJson({ ok: false, event: "fatal", error: String(err?.message || err) });
    process.exit(2);
  }
  let n = 0;
  try {
    const r = await doGroupMembers(api, args.group, (p) => {
      n += 1;
      if (p.phase === "start")
        emitJson({ ok: true, event: "start", groupId: args.group, name: p.name, total: p.total, chunks: p.chunks });
      else if (p.phase === "chunk")
        emitJson({ ok: true, event: "chunk", chunk: p.chunk, chunks: p.chunks, fetched: p.fetched, total: p.total });
    });
    emitJson({ ok: true, event: "done", ...r });
  } catch (err) {
    emitJson({ ok: false, event: "error", error: String(err?.message || err) });
    process.exit(1);
  }
  process.exit(0);
}

// ------------------------------------------------------------------- exports
// Reused by sessiond.mjs (single shared session owner). Function declarations
// above are hoisted, so this re-export block can sit here.
export { doFindUsers, doUserInfo, doFriendRequest, doSetAlias, doGroups, doGroupMembers, loginApi };

// ---------------------------------------------------------------------- main

async function main() {
  const argv = process.argv.slice(2);
  const cmd = argv.shift();
  const args = parseArgs(argv);
  switch (cmd) {
    case "login-qr": return cmdLoginQr(args);
    case "daemon": return cmdDaemon(args);
    case "whoami": return cmdWhoami(args);
    case "send": return cmdSend(args);
    case "find-users": return cmdFindUsers(args);
    case "user-info": return cmdUserInfo(args);
    case "friend-request": return cmdFriendRequest(args);
    case "set-alias": return cmdSetAlias(args);
    case "groups": return cmdGroups(args);
    case "group-members": return cmdGroupMembers(args);
    case "group-members-stream": return cmdGroupMembersStream(args);
    default:
      console.error("usage: node zalo.mjs <login-qr|daemon|whoami|send|find-users|friend-request|groups|group-members> [--args]");
      process.exit(2);
  }
}
import { fileURLToPath } from "node:url";

const __isMain = (() => {
  try {
    const self = fs.realpathSync(fileURLToPath(import.meta.url));
    const argv1 = process.argv[1] ? fs.realpathSync(process.argv[1]) : "";
    return !!argv1 && argv1 === self;
  } catch { return false; }
})();
if (__isMain) {
  main().catch((err) => {
    // CLI one-shots: emit a CLEAN JSON error (not a JS stack trace) so the
    // Python worker can classify it correctly and show a human message.
    const msg = String(err?.message || err);
    if (process.env.ZS_DEBUG) console.error(`FATAL: ${err?.stack || err}`);
    emitJson({ ok: false, error: msg });
    process.exit(2);
  });
}
