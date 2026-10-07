// Unit test: relay.mjs watch allowlist gating (bridge-side listener filter).
import { watchFrom, watchGroup, watchState, WATCH_GROUP_MSG_TYPES } from "../bridge/relay.mjs";

let fails = 0;
function check(name, cond, extra = "") {
  console.log((cond ? "OK  " : "FAIL") + "  " + name + (extra && !cond ? " :: " + extra : ""));
  if (!cond) fails++;
}

// default = all (standalone worker safety)
watchFrom([], "all");
check("default all: any group watched", watchGroup("123") === true && watchGroup("999") === true);
check("all mode state", watchState().mode === "all");

// only-mode gates precisely
watchFrom(["111", "222"], "only");
check("only: watched 111", watchGroup("111") === true);
check("only: watched 222", watchGroup("222") === true);
check("only: blocks 333", watchGroup("333") === false);
check("only: blocks empty gid", watchGroup("") === false);
check("only: numeric coercion", watchGroup(111) === true);
check("only mode state count", watchState().count === 2);

// dedupe / blanks dropped
watchFrom(["1", "1", "", null, "2"], "only");
check("dedupe + blanks dropped", watchState().count === 2, JSON.stringify(watchState()));

// mode string other than 'all' -> only
watchFrom(["5"], "weird");
check("non-all mode treated as only", watchState().mode === "only" && watchGroup("5") && !watchGroup("6"));

check("msgType set populated", WATCH_GROUP_MSG_TYPES.has("webchat") && WATCH_GROUP_MSG_TYPES.has("chat.photo"));

// --- mediaText: photo caption lives in content.title (real payload shape) ---
import { mediaText, classifyMedia as _cm } from "../bridge/relay.mjs";
const realPhoto = {
  title: "Chờ Bán (Short) LRCX26 Cà phê kỳ hạn tháng 11/2026\n\n👨💻Điểm vào lệnh : quanh 3570",
  description: "",
  href: "https://photo-stal-32.zdn.vn/gr/jpg/x.jpg",
  thumb: "https://photo-stal-32.zdn.vn/gr/jpg/x.jpg",
  params: '{"width":1368,"hd":"https://photo-stal-32.zdn.vn/gr/jpg/x.jpg","height":834}',
};
check("mediaText(photo) = title body (NOT empty)", mediaText("chat.photo", realPhoto).startsWith("Chờ Bán (Short) LRCX26"), JSON.stringify(mediaText("chat.photo", realPhoto)));
check("mediaText(photo) keeps full caption", mediaText("chat.photo", realPhoto).includes("Điểm vào lệnh : quanh 3570"));
check("mediaText falls back to description", mediaText("chat.photo", { title: "", description: "desc body", href: "h" }) === "desc body");
check("mediaText string content passthrough", mediaText("webchat", "hello world") === "hello world");
check("mediaText empty object -> ''", mediaText("chat.photo", { href: "h" }) === "");
check("mediaText no JSON dump for object", !mediaText("chat.photo", realPhoto).includes("{"));

console.log();
console.log("FAILS:", fails === 0 ? "none" : fails);
process.exit(fails ? 1 : 0);
