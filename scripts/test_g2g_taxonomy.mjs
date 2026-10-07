// Unit tests for relay.mjs canonical message taxonomy (normalizePart),
// coalescing (coalesceParts) and block building (buildBlocks). This is the
// single source of truth for how every Zalo message type becomes a post.
//
// Run:  node scripts/test_g2g_taxonomy.mjs
import { normalizePart, coalesceParts, buildBlocks, SYSTEM_MSG_TYPES } from "../bridge/relay.mjs";

let fails = 0;
function check(name, cond, extra = "") {
  console.log((cond ? "OK  " : "FAIL") + "  " + name + (extra && !cond ? " :: " + extra : ""));
  if (!cond) fails++;
}
const J = (x) => JSON.stringify(x);

// ---------------------------------------------------------------- text / link
check("plain text", J(normalizePart("webchat", "hello")) === J({ kind: "text", text: "hello" }));
check("empty text -> null", normalizePart("webchat", "  ") === null);

// ---------------------------------------------------------------- photo + caption
const photo = { title: "PT Coffe 11/2026\nEntry 3570", description: "",
  href: "https://photo-stal-32.zdn.vn/x.jpg", thumb: "https://photo-stal-32.zdn.vn/x.jpg",
  params: '{"hd":"https://photo-stal-32.zdn.vn/x.jpg","width":1368,"height":834}' };
const np = normalizePart("chat.photo", photo);
check("photo -> image + caption from title", np.kind === "image" && np.caption.startsWith("PT Coffe 11/2026"), J(np));
check("photo url picked", np.url === "https://photo-stal-32.zdn.vn/x.jpg");

// photo with NO caption still relays
const np2 = normalizePart("chat.photo", { href: "https://x/y.jpg", thumb: "https://x/y.jpg" });
check("captionless photo -> image, empty caption", np2.kind === "image" && np2.caption === "");

// ---------------------------------------------------------------- video / file / voice
const vid = normalizePart("chat.video.msg", { href: "https://x/v.mp4", thumb: "https://x/t.jpg",
  params: '{"duration":5500,"width":720,"height":1280}' });
check("video kind+url", vid.kind === "video" && vid.url === "https://x/v.mp4", J(vid));
check("video duration parsed", vid.extra.duration === 5500 && vid.extra.width === 720, J(vid));

const fil = normalizePart("share.file", { href: "https://x/doc.pdf", title: "doc.pdf", params: '{"totalSize":2048}' });
check("file kind+name", fil.kind === "file" && fil.extra.name === "doc.pdf" && fil.extra.size === 2048, J(fil));

check("voice kind+url", normalizePart("chat.voice", { href: "https://x/aac" }).kind === "voice");

// ---------------------------------------------------------------- sticker / gif / link / location
const st = normalizePart("chat.sticker", { params: '{"id":42,"cateId":7,"type":2}' });
check("sticker ids parsed", st.kind === "sticker" && st.extra.id === 42 && st.extra.cateId === 7, J(st));

check("gif kind", normalizePart("chat.gif", { href: "https://x/a.gif" }).kind === "gif");
check("link kind", normalizePart("chat.link", { href: "https://x/site", title: "Site" }).kind === "link");
check("location kind", normalizePart("chat.location.new", { title: "HCM" }).kind === "location");

// ---------------------------------------------------------------- system -> null
for (const mt of ["chat.undo", "chat.delete", "chat.reaction", "chat.typing", "chat.seen"]) {
  check(`system ${mt} -> null`, normalizePart(mt, { deleteMsg: 1 }) === null);
}
check("control frame -> null", normalizePart("chat.control", { act_type: "x" }) === null);

// ---------------------------------------------------------------- coalesce
// image + separate text -> single image with caption
const c1 = coalesceParts([{ kind: "image", url: "u1", caption: "" }, { kind: "text", text: "body" }]);
check("image+text coalesced (one part)", c1.length === 1 && c1[0].kind === "image" && c1[0].caption === "body", J(c1));

// image WITH caption then text -> text dropped (no dup)
const c2 = coalesceParts([{ kind: "image", url: "u1", caption: "cap" }, { kind: "text", text: "dup" }]);
check("captioned image drops following text", c2.length === 1 && c2[0].caption === "cap", J(c2));

// captionless images are NOT merged into an album any more — keeps the forward
// log 1:1 with the source (merging swallowed N msgIds into one part)
const c3 = coalesceParts([{ kind: "image", url: "u1", caption: "" }, { kind: "image", url: "u2", caption: "" }]);
check("captionless images stay as two parts", c3.length === 2 && c3[0].url === "u1" && c3[1].url === "u2", J(c3));

// image, text, image -> two parts (image+cap, image)
const c4 = coalesceParts([{ kind: "image", url: "u1", caption: "" }, { kind: "text", text: "cap" }, { kind: "image", url: "u2", caption: "" }]);
check("image,text,image -> [image+cap, image]", c4.length === 2 && c4[0].caption === "cap" && c4[1].kind === "image", J(c4));

// ---------------------------------------------------------------- buildBlocks (prefix only once)
const b1 = buildBlocks(coalesceParts([{ kind: "text", text: "first" }, { kind: "text", text: "second" }]), "#P ");
check("prefix only on first block", b1[0].content === "#P first" && b1[1].content === "second", J(b1));

const b2 = buildBlocks(coalesceParts([{ kind: "image", url: "u1", caption: "" }, { kind: "text", text: "cap" }]), "#P ");
check("image caption carries prefix", b2[0].type === "image" && b2[0].caption === "#P cap", J(b2));

const b3 = buildBlocks([{ kind: "video", url: "https://x/v.mp4", thumb: "t", caption: "v", extra: { duration: 100 } }], "#P ");
check("video block", b3[0].type === "video" && b3[0].caption === "#P v" && b3[0].extra.duration === 100, J(b3));

const b4 = buildBlocks([{ kind: "sticker", extra: { id: 3, cateId: 1, type: 2 } }], "#P ");
check("sticker block (no prefix needed)", b4[0].type === "sticker" && b4[0].extra.id === 3, J(b4));

check("SYSTEM_MSG_TYPES has undo", SYSTEM_MSG_TYPES.has("chat.undo"));

// ---------------------------------------------------------------- reply prefix block
// reply context is APPENDED BELOW the body, one blank line apart (same message)
const br = buildBlocks(coalesceParts([{ kind: "text", text: "body" }]), "#P ", "↩ 11:42:52: Mẫu nội dung tín hiệu A");
check("reply+text -> SINGLE block", br.length === 1, J(br));
check("reply context merged into the block", br[0].type === "text" && br[0].content.includes("↩ 11:42:52"), J(br));
check("body FIRST, reply context BELOW (blank line apart)",
  br[0].content === "#P body\n\n↩ 11:42:52: Mẫu nội dung tín hiệu A", J(br));

// reply attached to a CAPTIONED image -> context BELOW the caption
const brI = buildBlocks([{ kind: "image", url: "https://x/a.jpg", caption: "Ảnh mới" }], "#P ", "↩ 15:50:21: BÁN LƯỚT MHGZ26");
check("reply+captioned image -> SINGLE block", brI.length === 1, J(brI));
check("caption FIRST, reply context BELOW",
  brI[0].caption === "#P Ảnh mới\n\n↩ 15:50:21: BÁN LƯỚT MHGZ26", J(brI));

// captionless image + reply -> context becomes the caption
const brIc = buildBlocks([{ kind: "image", url: "https://x/a.jpg", caption: "" }], "#P ", "↩ 15:50:21: BÁN LƯỚT MHGZ26");
check("reply+captionless image -> context as caption", brIc[0].caption.includes("↩ 15:50:21:"), J(brIc));

// reply with no body parts -> standalone context text
const brOnly = buildBlocks([], "#P ", "↩ 10:00: hi");
check("reply-only -> 1 context block", brOnly.length === 1 && brOnly[0].content.includes("hi"), J(brOnly));

const br2 = buildBlocks([{ kind: "text", text: "body" }], "#P ", "");
check("no reply prefix -> prefix on body", br2[0].content === "#P body", J(br2));
check("prefix sits RIGHT BEFORE the new text", br[0].content.startsWith("#P body"), J(br[0].content));

console.log();
console.log("FAILS:", fails === 0 ? "none" : fails);
process.exit(fails ? 1 : 0);
