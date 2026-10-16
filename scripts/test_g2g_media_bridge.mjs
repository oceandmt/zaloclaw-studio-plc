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

// --- 4) FILE relay: a document must NOT be sent as an image -----------------
// Regression: downloadToTmp forces every non-image to `.jpg`, so a real
// .xlsx/.pdf used to reach zca-js with a .jpg name -> image upload path ->
// imageMetadataGetter fails -> the file silently degraded to a text caption.
// fetchFileAttachment must return an AttachmentSource {data, filename, metadata}
// that keeps the REAL extension (so zca-js routes to the "others"/file uploader)
// and the original filename.
const fsrv = http.createServer((req, res) => {
  res.writeHead(200, { "content-type": "application/octet-stream" });
  res.end(Buffer.concat([Buffer.from("PK\x03\x04"), Buffer.alloc(300, 7)]));
});
await new Promise((r) => fsrv.listen(0, "127.0.0.1", r));
const FILE_URL = `http://127.0.0.1:${fsrv.address().port}/bao%20cao%20tong%20hop.xlsx`;

const att = await R.fetchFileAttachment(FILE_URL, "nickT", "báo cáo tổng hợp.xlsx");
ck("fetchFileAttachment -> AttachmentSource with Buffer data", att && Buffer.isBuffer(att.data) && att.data.length > 0);
ck("fetchFileAttachment keeps REAL extension (.xlsx, not .jpg)", att.filename === "báo cáo tổng hợp.xlsx", String(att.filename));
ck("fetchFileAttachment sets metadata.totalSize", att.metadata && att.metadata.totalSize === att.data.length, JSON.stringify(att.metadata));

// filename fallback from the URL when none is supplied
const att2 = await R.fetchFileAttachment(FILE_URL, "nickT", "");
ck("filename falls back to URL basename", /\.xlsx$/.test(att2.filename) && att2.filename.length > 0, String(att2.filename));

// a file block must reach sendMessage as ONE attachment whose filename is the file
calls.length = 0;
const api2 = {
  sendMessage: async (m, thread, type) => {
    calls.push({ msg: m.msg, attachments: m.attachments, thread, type });
    return m.attachments?.length ? { attachment: [{ msgId: "FILE1" }] } : { message: { msgId: "TXT1" } };
  },
  sendLink: async () => ({ msgId: "LNK1" }),
};
const outF = await R.sendBlocks(api2, [{ type: "file", url: FILE_URL, caption: "15.10. DANH SÁCH HÀNG HÓA.xlsx", extra: { name: "15.10. DANH SÁCH HÀNG HÓA.xlsx" } }], "G_B", 1, "nickT");
ck("file block -> exactly 1 sendMessage", calls.length === 1, JSON.stringify(calls));
ck("file block attachment is an AttachmentSource (not a .jpg path)",
   calls[0]?.attachments?.[0] && typeof calls[0].attachments[0] === "object" && Buffer.isBuffer(calls[0].attachments[0].data));
ck("file block attachment filename keeps .xlsx",
   calls[0]?.attachments?.[0]?.filename === "15.10. DANH SÁCH HÀNG HÓA.xlsx", String(calls[0]?.attachments?.[0]?.filename));
ck("file block caption preserved as msg", calls[0]?.msg === "15.10. DANH SÁCH HÀNG HÓA.xlsx");
ck("file block returns file msgId", outF.msgId === "FILE1", JSON.stringify(outF));

// a dead file URL must fall back to a text send (never drop the post)
calls.length = 0;
const errs2 = [];
await R.sendBlocks(api2, [{ type: "file", url: "http://127.0.0.1:1/x.xlsx", caption: "cap" }], "G_B", 1, "nickT", (e) => errs2.push(e));
ck("dead file url -> text fallback + error reported", calls.length === 1 && calls[0].attachments === undefined && errs2.length === 1, JSON.stringify({ calls, errs2 }));

fsrv.close();

srv.close();
try { fs.unlinkSync(tmpMod); } catch { /* ignore */ }

console.log("\n" + (fails ? "FAILS: see above" : "ALL PASS"));
process.exit(fails ? 1 : 0);
