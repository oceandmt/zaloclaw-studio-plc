// Media-relay helpers for the G2G autobot.
//
// A group photo/video message arrives as an OBJECT `content` (with a remote
// URL), not plain text. These helpers extract that URL, classify the media,
// download it locally (so zca-js can re-upload it) and send ordered
// text/image/link blocks — mirroring the campaign sender's doSend logic.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { ThreadType } from "zca-js";

const IMG_EXT = { jpg: 1, jpeg: 1, png: 1, webp: 1, gif: 1 };
export const isHttpUrl = (u) => typeof u === "string" && /^https?:\/\//i.test(u);

export function pickMediaUrl(c) {
  if (!c) return "";
  if (typeof c === "string") {
    const s = c.trim();
    if (isHttpUrl(s)) return s;
    if (s.startsWith("{") || s.startsWith("[")) {
      try { return pickMediaUrl(JSON.parse(s)); } catch { return ""; }
    }
    return "";
  }
  if (typeof c !== "object") return "";
  // Prefer higher-quality sources first; thumbnail last.
  for (const k of ["oriUrl", "hdUrl", "rawUrl", "normalUrl", "href", "fileUrl", "thumbUrl", "thumb"]) {
    if (isHttpUrl(c[k])) return c[k];
  }
  if (c.params) {
    try { return pickMediaUrl(typeof c.params === "string" ? JSON.parse(c.params) : c.params); } catch { /* ignore */ }
  }
  return "";
}

export function classifyMedia(msgType, content) {
  const mt = String(msgType || "");
  if (/video/i.test(mt)) return "video";
  if (/photo|image|gif/i.test(mt)) return "image";
  // object content that carries an image-ish URL is a photo even without msgType
  if (content && typeof content === "object" && (content.href || content.oriUrl || content.thumb)) return "image";
  return "";
}

// Best-effort human text of an incoming message. Zalo puts a photo/file
// message's typed caption in `content.title` (there is NO `caption` key on the
// real payload) — so title must be checked FIRST or the body silently drops.
export function mediaText(msgType, content) {
  if (typeof content === "string") return content;
  if (!content || typeof content !== "object") return "";
  const cands = [content.title, content.description,
    typeof content.content === "string" ? content.content : ""];
  for (const c of cands) {
    if (typeof c === "string" && c.trim()) return c;
  }
  // Non-media object with no usable text: don't dump the whole JSON blob.
  return classifyMedia(msgType, content) ? "" : "";
}

// ---- Canonical message normalisation -------------------------------------
// Every incoming Zalo message is reduced to ONE canonical "Part" so the rest
// of the pipeline never has to know Zalo's msgType zoo. Kinds:
//   text | image | gif | video | file | voice | sticker | link | location |
//   recommended | unknown   (null = system event -> skip)
// The hard-won lessons encoded here:
//   * a photo's typed body lives in `content.title` (never `caption`)
//   * `params` is a JSON string holding hd/width/height (and sticker ids)
//   * system frames (recall/delete/join/react) must be dropped, not relayed
export const SYSTEM_MSG_TYPES = new Set([
  "chat.undo", "chat.delete", "chat.reaction", "chat.react", "chat.typing",
  "chat.delivered", "chat.seen", "chat.group", "chat.ecard",
]);

function _params(c) {
  if (!c || typeof c !== "object" || !c.params) return {};
  try { return typeof c.params === "string" ? JSON.parse(c.params) : (c.params || {}); }
  catch { return {}; }
}

function _isSystem(msgType, content) {
  const mt = String(msgType || "");
  if (SYSTEM_MSG_TYPES.has(mt)) return true;
  if (mt === "chat.control" || mt === "control") return true;
  if (content && typeof content === "object" &&
      ("deleteMsg" in content || "delete_member_id" in content || "act_type" in content)) return true;
  return false;
}

// msgType -> {kind, url, caption, thumb, extra} | null (system)
export function normalizePart(msgType, content) {
  const mt = String(msgType || "").toLowerCase();
  if (_isSystem(msgType, content)) return null;

  // Plain text (webchat) — a link card is also delivered as webchat+object.
  if (typeof content === "string") {
    if (!content.trim()) return null;
    if (mt === "chat.link") return { kind: "link", url: "", caption: content };
    return { kind: "text", text: content };
  }
  if (!content || typeof content !== "object") return { kind: "text", text: "" };

  const p = _params(content);
  const url = pickMediaUrl(content);
  const cap = mediaText(msgType, content);

  if (mt === "chat.sticker") {
    const id = Number(p.id ?? content.id ?? 0);
    const cateId = Number(p.cateId ?? p.catId ?? content.cateId ?? 0);
    const type = Number(p.type ?? content.type ?? 0);
    if (id) return { kind: "sticker", extra: { id, cateId, type } };
    return { kind: "unknown", text: "[sticker]" };
  }
  if (mt.includes("voice")) return { kind: "voice", url };
  if (mt.includes("video")) {
    return { kind: "video", url, caption: cap,
      thumb: content.thumb || content.thumbUrl || p.thumb || "",
      extra: { duration: p.duration ?? content.duration ?? 0,
               width: p.width ?? 0, height: p.height ?? 0 } };
  }
  if (mt === "chat.gif" || /gif/i.test(mt)) return { kind: "gif", url, caption: cap };
  if (mt === "share.file") {
    return { kind: "file", url, caption: cap,
      extra: { name: content.title || p.fileName || "file",
               size: Number(p.totalSize ?? p.size ?? 0) } };
  }
  if (mt === "chat.link") return { kind: "link", url, caption: cap };
  if (mt === "chat.location.new" || /location/i.test(mt)) {
    return { kind: "location", text: cap || (url ? `📍 ${url}` : "[vị trí]") };
  }
  if (mt === "chat.recommended") return { kind: "recommended", text: cap, url };
  if (mt.includes("photo") || mt === "photo" || mt === "chat.image") {
    return { kind: "image", url, caption: cap };
  }
  // Unknown object: keep it if it carries media, else fall back to its text.
  if (url) return { kind: "image", url, caption: cap };
  if (cap) return { kind: "text", text: cap };
  return { kind: "unknown", text: "" };
}

// Coalesce adjacent parts that belong to one visual post:
//   * an image with no caption immediately followed by text  -> fold text in
//   * a run of captionless images                            -> one album part
// `windowMs` is enforced by the caller (time-based grouping).
export function coalesceParts(parts) {
  const out = [];
  for (const pt of (parts || []).filter(Boolean)) {
    const last = out[out.length - 1];
    const isImg = (k) => k === "image" || k === "gif";
    if (pt.kind === "text" && last && isImg(last.kind) && !String(last.caption || "").trim()) {
      last.caption = pt.text;               // caption attaches to the image
      continue;
    }
    if (pt.kind === "text" && last && isImg(last.kind)) {
      continue;                             // image already had a caption -> drop dup text
    }
    // NOTE: captionless images are NOT merged into an album any more. Merging
    // them silently swallowed N source messages into ONE part, so only the last
    // msgId survived and the rest never appeared in the forward log (audit
    // showed 23 received vs 26 logged). Each image now stays its own part (and
    // therefore its own send-block), keeping the trail 1:1 with the source.
    out.push({ ...pt });
  }
  return out;
}

// Turn canonical parts into bridge send-blocks. Only the FIRST content block
// carries the rule prefix so a multi-part post isn't spammed with it. A reply's
// context line (`replyPrefix`) is APPENDED BELOW the body, separated by ONE
// blank line, inside the same message — never emitted as a separate message.
export function buildBlocks(parts, prefix = "", replyPrefix = "") {
  const blocks = [];
  let usedPrefix = false;
  const rp = String(replyPrefix || "").trim();
  let replyLeft = rp;
  // Append the reply context inside the block it belongs to: text -> an extra
  // line BELOW the text, image/file/video -> a caption line BELOW the caption
  // (Zalo carries the image caption). The rule prefix rides the very first
  // block, so everything lands in ONE message.
  const withPrefix = (s) => {
    const raw = String(s || "");
    let body = raw;
    if (!usedPrefix) { usedPrefix = true; body = (prefix || "") + body; }
    if (replyLeft) {
      const ctx = replyLeft; replyLeft = "";
      // Context line sits BELOW the new content, one blank line between them.
      return raw ? (body + "\n\n" + ctx) : ctx;
    }
    return body;
  };
  for (const pt of (parts || []).filter(Boolean)) {
    if (pt.kind === "text") {
      if (String(pt.text || "").trim()) blocks.push({ type: "text", content: withPrefix(pt.text) });
    } else if (pt.kind === "image" || pt.kind === "gif") {
      const urls = (pt.urls && pt.urls.length ? pt.urls : [pt.url]).filter(Boolean);
      if (urls.length) blocks.push({ type: "image", urls, caption: withPrefix(pt.caption || "") });
    } else if (pt.kind === "video") {
      if (pt.url) blocks.push({ type: "video", url: pt.url, thumb: pt.thumb || "",
        caption: withPrefix(pt.caption || ""), extra: pt.extra || {} });
    } else if (pt.kind === "file") {
      if (pt.url) blocks.push({ type: "file", url: pt.url, caption: withPrefix(pt.caption || ""),
        extra: pt.extra || {} });
    } else if (pt.kind === "voice") {
      if (pt.url) blocks.push({ type: "voice", url: pt.url });
    } else if (pt.kind === "sticker") {
      blocks.push({ type: "sticker", extra: pt.extra || {} });
    } else if (pt.kind === "link") {
      if (pt.url) blocks.push({ type: "link", url: pt.url, caption: withPrefix(pt.caption || "") });
      else if (String(pt.caption || "").trim()) blocks.push({ type: "text", content: withPrefix(pt.caption) });
    } else if (pt.kind === "location" || pt.kind === "recommended" || pt.kind === "unknown") {
      const t = pt.text || pt.caption || "";
      if (String(t).trim()) blocks.push({ type: "text", content: withPrefix(t) });
    }
  }
  // Ensure at least one block for an image-only post with no caption.
  if (!blocks.length && parts && parts.some((p) => p && (p.url || p.urls))) {
    for (const pt of parts) {
      if (pt.url || pt.urls) {
        blocks.push({ type: "image", urls: pt.urls || [pt.url], caption: withPrefix("") });
        break;
      }
    }
  }
  // Reply context with nothing to attach to (no body parts) -> standalone text.
  if (replyLeft) {
    const t = replyLeft; replyLeft = "";
    blocks.push({ type: "text", content: usedPrefix ? t : ((prefix || "") + t) });
  }
  return blocks;
}

// ---- Group watch allowlist ----------------------------------------------
// The panel only ever forwards messages from the SOURCE groups that are wired
// into an enabled pipeline. We still must open ONE websocket per account, but
// we can cheaply ignore activity from every other group inside the worker so
// unrelated messages are never emitted, dumped, or handed to the controller.

// msgType values for group (not 1:1) messages we treat as watched content.
// (webchat = text/link card, chat.photo|photo = image, chat.video* = video,
//  share.file = file, chat.recommended = forward, webchat.quote = reply.)
export const WATCH_GROUP_MSG_TYPES = new Set([
  "webchat", "chat.photo", "photo", "chat.video.msg", "chat.video",
  "share.file", "chat.recommended", "chat.sticker", "chat.gif",
  "chat.voice", "webchat.quote",
]);

// A small LRU of watched group ids. Built with watchFrom(ids, mode).
const _watch = { mode: "all", ids: new Set() };

export function watchFrom(ids, mode) {
  const m = mode === "all" ? "all" : "only";
  const set = new Set(
    (ids || [])
      .map((x) => (x === null || x === undefined ? "" : String(x).trim()))
      .filter((s) => s && s !== "null" && s !== "undefined"),
  );
  _watch.mode = m;
  _watch.ids = set;
  return { mode: m, count: set.size };
}

export function watchState() {
  return { mode: _watch.mode, count: _watch.ids.size, ids: [..._watch.ids] };
}

// True when a group message SHOULD be emitted/dumped/handled.
export function watchGroup(gid) {
  if (_watch.mode === "all") return true;
  if (!gid) return false;
  return _watch.ids.has(String(gid));
}

const _mediaCache = new Map();
export async function downloadToTmp(url, account) {
  const hit = _mediaCache.get(url);
  if (hit && fs.existsSync(hit)) return hit;
  let ext = (String(url).match(/\.([a-z0-9]{2,4})(?:[?#]|$)/i)?.[1] || "jpg").toLowerCase();
  if (!IMG_EXT[ext]) ext = "jpg";
  const dir = path.join(os.tmpdir(), "zsfwd");
  fs.mkdirSync(dir, { recursive: true });
  const out = path.join(dir, `${account || "a"}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.${ext}`);
  const r = await fetch(url, { headers: { "user-agent": "Mozilla/5.0" }, redirect: "follow" });
  if (!r.ok) throw new Error(`tải media lỗi HTTP ${r.status}`);
  const buf = Buffer.from(await r.arrayBuffer());
  if (!buf.length) throw new Error("media rỗng");
  fs.writeFileSync(out, buf);
  _mediaCache.set(url, out);
  if (_mediaCache.size > 200) { const [k] = _mediaCache.keys(); _mediaCache.delete(k); }
  return out;
}

// Send an ordered block list (text/image/link). Returns { ok, msgId, msgIds, images }.
// `quote`, when present, is attached to the FIRST message sent (native reply).
export async function sendBlocks(api, blocks, thread, type, account, onError, quote) {
  // zca-js indexes its message service URLs by the NUMERIC ThreadType enum
  // (User=0 / Group=1). The wire protocol carries the string "group"/"user",
  // and feeding that straight to the API makes it build `new URL(undefined)`
  // -> "TypeError: Invalid URL" on every send. Normalise here (the single choke
  // point for all block sends) so callers can pass either form.
  const ttype = (type === "group" || type === "1" || type === 1) ? ThreadType.Group : ThreadType.User;
  const ids = [];
  let nimg = 0;
  let q = quote || null;               // consumed once, on the first send
  const withQuote = (o) => (q ? { ...o, quote: q } : o);
  const consume = () => { q = null; };
  for (const b of (Array.isArray(blocks) ? blocks : []).filter(Boolean)) {
    const bt = String(b.type || "").toLowerCase();
    if (bt === "text") {
      const t = String(b.content ?? "");
      if (!t) continue;
      const r = await api.sendMessage(withQuote({ msg: t }), thread, ttype);
      consume();
      const m = r?.message?.msgId ?? r?.msgId ?? "";
      if (m) ids.push(String(m));
    } else if (bt === "image") {
      const urls = (Array.isArray(b.urls) ? b.urls : [b.url]).filter(Boolean);
      if (!urls.length) continue;
      const files = [];
      for (const u of urls) {
        try { files.push(await downloadToTmp(u, account)); }
        catch (e) { if (onError) onError(String(e?.message || e)); }
      }
      if (!files.length) continue;
      const cap = String(b.caption ?? "");
      const r = await api.sendMessage(withQuote({ msg: cap, attachments: files }), thread, ttype);
      consume();
      const m = r?.attachment?.[0]?.msgId ?? r?.message?.msgId ?? r?.msgId ?? "";
      if (m) ids.push(String(m));
      nimg += files.length;
    } else if (bt === "link") {
      const u = String(b.url ?? "");
      if (!u) continue;
      const cap = String(b.caption ?? "");
      const r = await api.sendLink({ link: u, ...(cap ? { msg: cap } : {}) }, thread, ttype);
      const m = r?.msgId ?? r?.message?.msgId ?? "";
      if (m) ids.push(String(m));
    } else if (bt === "video") {
      const u = String(b.url ?? "");
      if (!u) continue;
      try {
        const thumb = String(b.thumb ?? "") || u;
        const r = await api.sendVideo({ videoUrl: u, thumbnailUrl: thumb,
          msg: String(b.caption ?? ""), duration: b.extra?.duration || undefined,
          width: b.extra?.width || undefined, height: b.extra?.height || undefined }, thread, ttype);
        const m = r?.msgId ?? r?.message?.msgId ?? "";
        if (m) ids.push(String(m));
      } catch (e) {
        // Fallback: never drop the post — send the caption (or the URL) as text.
        if (onError) onError(String(e?.message || e));
        const t = String(b.caption || u);
        if (t) { const r = await api.sendMessage({ msg: t }, thread, ttype); const m = r?.message?.msgId ?? r?.msgId ?? ""; if (m) ids.push(String(m)); }
      }
    } else if (bt === "file") {
      const u = String(b.url ?? "");
      if (!u) continue;
      try {
        const f = await downloadToTmp(u, account);
        const r = await api.sendMessage({ msg: String(b.caption ?? ""), attachments: [f] }, thread, ttype);
        const m = r?.attachment?.[0]?.msgId ?? r?.message?.msgId ?? r?.msgId ?? "";
        if (m) ids.push(String(m));
      } catch (e) {
        if (onError) onError(String(e?.message || e));
        const t = [String(b.caption || ""), u].filter(Boolean).join("\n");
        const r = await api.sendMessage({ msg: t }, thread, ttype); const m = r?.message?.msgId ?? r?.msgId ?? ""; if (m) ids.push(String(m));
      }
    } else if (bt === "voice") {
      const u = String(b.url ?? "");
      if (!u) continue;
      try {
        const r = await api.sendVoice({ voiceUrl: u }, thread, ttype);
        const m = r?.msgId ?? r?.message?.msgId ?? "";
        if (m) ids.push(String(m));
      } catch (e) { if (onError) onError(String(e?.message || e)); }
    } else if (bt === "sticker") {
      const ex = b.extra || {};
      if (!ex.id) continue;
      try {
        const r = await api.sendSticker({ id: ex.id, cateId: ex.cateId || 0, type: ex.type || 0 }, thread, ttype);
        const m = r?.msgId ?? r?.message?.msgId ?? "";
        if (m) ids.push(String(m));
      } catch (e) { if (onError) onError(String(e?.message || e)); }
    }
  }
  return { ok: true, msgId: ids[ids.length - 1] || "", msgIds: ids, images: nimg };
}

// Turn one raw zca-js message into the canonical event the controller consumes.
// Moved here (from autobot.mjs) so it can be shared by BOTH the live listener
// and the single-session daemon without importing autobot's main loop.
export function msgToEvent(message, account) {
  const d = message?.data || {};
  const isGroup = message?.type === 1;
  const gid = message?.threadId || d.idTo || "";
  const part = normalizePart(d.msgType, d.content);
  if (!part) return null; // system frame: never relay
  const mediaKind = (part.kind === "image" || part.kind === "gif" || part.kind === "video") ? part.kind : "";
  const media = part.url ? [part.url] : [];
  const text = part.text ?? part.caption ?? "";
  const quote = d.quote ? {
    ownerId: d.quote.ownerId ?? null,
    cliMsgId: d.quote.cliMsgId ?? null,
    msgId: d.quote.msgId ?? null,
    globalMsgId: d.quote.globalMsgId ?? null,
    cliMsgType: d.quote.cliMsgType ?? null,
    ts: d.quote.ts ?? null,
    msg: typeof d.quote.msg === "string" ? d.quote.msg : null,
    attach: typeof d.quote.attach === "string" ? d.quote.attach : (d.quote.attach ?? null),
    fromD: d.quote.fromD ?? null,
    ttl: d.quote.ttl ?? null,
  } : null;
  return {
    isGroup, gid,
    event: {
      event: "msg",
      account,
      threadId: message?.threadId || "",
      group: isGroup ? gid : "",
      idTo: d.idTo || "",
      msgId: d.msgId || "",
      realMsgId: d.realMsgId || "",
      uidFrom: d.uidFrom || "",
      name: d.dName || "",
      isSelf: !!message?.isSelf,
      ts: d.ts || "",
      srcTs: Number(d.ts) || 0,
      msgType: d.msgType || "",
      part,
      mediaKind,
      media,
      quote,
      text: String(text ?? ""),
    },
  };
}
