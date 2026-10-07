// Bridge-side regression tests for the G2G media path.
//   node scripts/test_g2g_media_bridge.mjs
// Verifies the two failure modes that broke image forwarding:
//   1) Zalo options MUST carry imageMetadataGetter (else every attachment send
//      throws ZaloApiMissingImageMetadataGetter).
//   2) relay.sendBlocks must upload the downloaded file as an attachment AND
//      keep the caption.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import http from "node:http";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const BRIDGE = path.join(ROOT, "bridge");

let fails = 0;
const ck = (name, cond, extra = "") => {
  console.log((cond ? "OK  " : "FAIL") + "  " + name + (cond ? "" : " :: " + extra));
  if (!cond) fails++;
};

// --- load the REAL helper section of autobot.mjs (pre-IIFE) -----------------
const src = fs.readFileSync(path.join(BRIDGE, "autobot.mjs"), "utf8");
const cut = src.indexOf("(async () => {");
const head = src.slice(0, cut)
  .replace(/^import .*zca-js.*$/m, "")
  .replace(/^import readline.*$/m, "")
const tmpMod = path.join(BRIDGE, "_autobot_head.mjs");
fs.writeFileSync(tmpMod, head + "\nexport { imageMetadataGetter, zaloOptions };\n");

const A = await import(tmpMod);
const R = await import(path.join(BRIDGE, "relay.mjs"));

// --- 1) imageMetadataGetter present + working ------------------------------
const z = A.zaloOptions();
ck("zaloOptions() provides imageMetadataGetter", typeof z.imageMetadataGetter === "function");
ck("zaloOptions() keeps selfListen/logging", z.selfListen === true && z.logging === false);

// tiny valid 2x3 PNG
const png = Buffer.from(
  "89504e470d0a1a0a0000000d494844520000000200000003080600000000" +
  "c9f0f1f10000000649444154789c63f8cf000003" +
  "01010018dd8db00000000049454e44ae426082", "hex");
const p = path.join(os.tmpdir(), "zs_imgtest.png");
fs.writeFileSync(p, png);
const md = await (z.imageMetadataGetter || A.imageMetadataGetter)(p);
ck("imageMetadataGetter returns width/height/size", md && md.width === 2 && md.height === 3 && md.size === png.length, JSON.stringify(md));
ck("imageMetadataGetter returns null on missing file", (await A.imageMetadataGetter("/no/such/file.png")) === null);

// --- 2) sendBlocks uploads attachment + caption ----------------------------
const srv = http.createServer((_, res) => { res.writeHead(200, { "content-type": "image/jpeg" }); res.end(Buffer.from([0xFF, 0xD8, 0xFF, 0xE0, 1, 2, 3, 4])); });
await new Promise((r) => srv.listen(0, "127.0.0.1", r));
const IMG = `http://127.0.0.1:${srv.address().port}/p.jpg`;

const calls = [];
const api = {
  sendMessage: async (m, thread, type) => {
    calls.push({ msg: m.msg, attachments: m.attachments, thread, type });
    return m.attachments?.length ? { attachment: [{ msgId: "IMG1" }] } : { message: { msgId: "TXT1" } };
  },
  sendLink: async (m) => { calls.push({ link: m.link }); return { msgId: "LNK1" }; },
};

calls.length = 0;
const out = await R.sendBlocks(api, [{ type: "image", urls: [IMG], caption: "Giá thép" }], "G_B", 1, "nickT");
ck("image block -> exactly 1 sendMessage", calls.length === 1, JSON.stringify(calls));
ck("image block carries an attachment file", Array.isArray(calls[0]?.attachments) && calls[0].attachments.length === 1);
ck("attachment file actually exists on disk", calls[0]?.attachments?.[0] && fs.existsSync(calls[0].attachments[0]));
ck("caption preserved as msg", calls[0]?.msg === "Giá thép");
ck("returns image msgId + count", out.msgId === "IMG1" && out.images === 1, JSON.stringify(out));

calls.length = 0;
await R.sendBlocks(api, [{ type: "image", urls: [IMG] }], "G_B", 1, "nickT");
ck("image without caption -> empty msg", calls[0]?.msg === "");

// --- 3) thread-type normalisation (Invalid URL regression) -----------------
// zca-js indexes service URLs by the NUMERIC ThreadType enum; passing the wire
// string "group"/"user" straight through threw "TypeError: Invalid URL". The
// choke point must emit 1 for group and 0 for user, accepting both wire forms.
calls.length = 0;
await R.sendBlocks(api, [{ type: "text", content: "hi" }], "G_B", "group", "nickT");
ck("string 'group' -> numeric ThreadType.Group(1)", calls[0]?.type === 1, JSON.stringify(calls[0]?.type));
calls.length = 0;
await R.sendBlocks(api, [{ type: "text", content: "hi" }], "U_B", "user", "nickT");
ck("string 'user' -> numeric ThreadType.User(0)", calls[0]?.type === 0, JSON.stringify(calls[0]?.type));
calls.length = 0;
await R.sendBlocks(api, [{ type: "text", content: "hi" }], "U_B", undefined, "nickT");
ck("undefined type defaults to User(0)", calls[0]?.type === 0, JSON.stringify(calls[0]?.type));

// dead URL -> graceful, no send
calls.length = 0;
const errs = [];
const out2 = await R.sendBlocks(api, [{ type: "image", urls: ["http://127.0.0.1:1/x.jpg"] }], "G_B", 1, "nickT", (e) => errs.push(e));
ck("dead media url -> no send + error reported", calls.length === 0 && errs.length === 1 && out2.images === 0, JSON.stringify({ calls, errs }));

srv.close();
try { fs.unlinkSync(tmpMod); } catch { /* ignore */ }

console.log("\n" + (fails ? "FAILS: see above" : "ALL PASS"));
process.exit(fails ? 1 : 0);
