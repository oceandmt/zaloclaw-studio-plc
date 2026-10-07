#!/usr/bin/env python3
"""Regression: REPLY handling for the G2G forwarder (Mức 1 + optional native quote).

Covers the Python side of the reply feature:
  * _reply_quote_text() renders a one-line excerpt of ANY parent kind
    (text / photo-attach / voice) from a REAL captured `quote` payload
  * _reply_prefix() builds the "↩ name · hh:mm: <quote>" context line and,
    in 'quote' mode, a native quote object when the parent maps to the dest
  * _bot_dispatch_part(): 'text' prepends the context block, 'skip' drops the
    reply, and the parent is mapped via forward_log (14-digit -> direct)
  * _fwd_send_cmd() carries reply_prefix + quote into the bridge `send`
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "webapp"))
import core  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + "  " + name + ((" :: " + extra) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


# ---- real captured quote payloads (from data/capture_raw.ndjson) ----------
Q_TEXT = {
    "ownerId": "<USER_ID_A>", "cliMsgId": "1790916171629", "msgId": None,
    "globalMsgId": "8328703376989", "cliMsgType": 1, "ts": 1790916172061,
    "msg": "Mẫu nội dung tín hiệu A",
    "attach": "", "fromD": "<TEN_KHACH_A>", "ttl": 0,
}
Q_PHOTO = {
    "ownerId": "<USER_ID_B>", "cliMsgId": "1790911265080", "msgId": None,
    "globalMsgId": "8328340265545", "cliMsgType": 32, "ts": 1790911329873, "msg": "",
    "attach": json.dumps({"title": "Mẫu tiêu đề tín hiệu B\n\n👨‍💻Điểm vào lệnh : Quanh 676-678",
                          "href": "https://photo-stal-15.zdn.vn/gr/jpg/x.jpg",
                          "thumbUrl": "https://photo-stal-15.zdn.vn/gr/jpg/x.jpg"}, ensure_ascii=False),
    "fromD": "<TEN_KHACH_B>", "ttl": 0,
}
Q_VOICE = {"ownerId": "u9", "cliMsgId": "1", "globalMsgId": "9", "cliMsgType": 31,
           "ts": 1790916172061, "msg": "", "attach": "", "fromD": "Ai đó", "ttl": 0}

# --- 1) excerpt rendering ---------------------------------------------------
check("text quote -> excerpt", "Mẫu nội dung tín hiệu A" in core._reply_quote_text(Q_TEXT))
check("photo quote -> title excerpt", "Mẫu tiêu đề tín hiệu B" in core._reply_quote_text(Q_PHOTO))
check("photo quote keeps newline out (one line)",
      "\n" not in core._reply_quote_text(Q_PHOTO))
check("voice quote -> [thoại]", core._reply_quote_text(Q_VOICE) == "[thoại]")

# --- 2) context line (text mode) with original timestamp --------------------
RULE_TEXT = {"id": "f_ab667dc0", "account_id": "nick1", "reply_mode": "text", "reply_quote_ms": 1}

# same-day parent -> compact hh:mm:ss (no date)
Q_SAMEDAY = {**Q_TEXT, "ts": int(time.time() * 1000)}
q_exp = time.strftime("%H:%M:%S", time.localtime(Q_SAMEDAY["ts"] / 1000.0))
blk, qobj = core._reply_prefix(Q_SAMEDAY, RULE_TEXT, "f_ab667dc0", "nick1", "<DST_GROUP_ID>")
check("context has NO original sender name", "<TEN_KHACH_A>" not in blk)
check("same-day context = ↩ + hh:mm:ss: <quote>", blk.startswith(f"↩ {q_exp}: "), blk)
check("same-day context has NO date", "/" not in blk, blk)
check("context carries the excerpt", "Mẫu nội dung tín hiệu A" in blk)
check("text mode -> no native quote obj", qobj is None)

# older-day parent -> date IS prefixed (dd/mm/YYYY hh:mm:ss)
Q_OLDAY = {**Q_TEXT, "ts": int((time.time() - 2 * 86400) * 1000)}
q_old_exp = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(Q_OLDAY["ts"] / 1000.0))
blk_old, _ = core._reply_prefix(Q_OLDAY, RULE_TEXT, "f_ab667dc0", "nick1", "<DST_GROUP_ID>")
check("older-day context = ↩ + dd/mm/YYYY hh:mm:ss:", blk_old.startswith(f"↩ {q_old_exp}: "), blk_old)
check("older-day context still carries the excerpt", "Mẫu nội dung tín hiệu A" in blk_old)

# timestamp OFF -> no clock
blk2, _ = core._reply_prefix(Q_TEXT, {**RULE_TEXT, "reply_quote_ms": 0},
                             "f_ab667dc0", "nick1", "g")
check("reply_quote_ms=0 -> no clock in context", "11:42:52" not in blk2, blk2)

# --- 3) native quote mode: mapped parent (14-digit -> direct) ---------------
RULE_QUOTE = {"id": "f_ab667dc0", "account_id": "nick1", "reply_mode": "quote", "reply_quote_ms": 1}
core.db = type("DB", (), {
    "find_forward_by_src": lambda self=None, *a, **k: {"dst_msg_id": "8328703386778"},
    "msgzilla_lookup": lambda self=None, *a, **k: None,
})()
blk3, qobj3 = core._reply_prefix(Q_TEXT, RULE_QUOTE, "f_ab667dc0", "nick1", "<DST_GROUP_ID>")
check("quote mode -> native quote obj built", isinstance(qobj3, dict))
if isinstance(qobj3, dict):
    check("native quote msgId = parent dst copy", qobj3.get("msgId") == "8328703386778")
    check("native quote carries uidFrom (owner)", str(qobj3.get("uidFrom")) == "<USER_ID_A>")
    check("native quote msgType passthrough", qobj3.get("msgType") == "webchat")

# unmapped parent (no forward_log row) -> graceful fallback to text
core.db = type("DB", (), {
    "find_forward_by_src": lambda self=None, *a, **k: None,
    "msgzilla_lookup": lambda self=None, *a, **k: None,
})()
blk4, qobj4 = core._reply_prefix(Q_TEXT, RULE_QUOTE, "f_ab667dc0", "nick1", "g")
check("quote mode, unmapped -> falls back to text (no obj)", qobj4 is None and blk4.startswith("↩"))
check("fallback context also omits sender name", "<TEN_KHACH_A>" not in blk4)

# --- 4) _fwd_send_cmd carries reply fields ---------------------------------
core.db = type("DB", (), {})()


def _rule(mode):
    return {"id": "f_r", "account_id": "nickZ", "enabled": 1, "src_group_id": "G_A",
            "dst_group_id": "G_B", "include_self": 0, "prefix": "#P ",
            "reply_mode": mode, "reply_quote_ms": 1, "group_window_ms": 0, "gap_seconds": 0}


cmd = core._fwd_send_cmd("G_B", "body", [{"kind": "text", "text": "body"}], "#P ", True,
                         reply_prefix="↩ X · 10:00: hi", quote={"msgId": "1", "uidFrom": "u"})
check("send cmd carries reply_prefix", cmd.get("reply_prefix") == "↩ X · 10:00: hi")
check("send cmd carries native quote", isinstance(cmd.get("quote"), dict))
check("send cmd keeps parts + prefix", cmd.get("parts") and cmd.get("prefix") == "#P ")

cmd2 = core._fwd_send_cmd("G_B", "just text", [], "", True, reply_prefix="↩ ctx")
check("legacy path embeds reply_prefix into text", cmd2.get("text", "").startswith("↩ ctx"))

# --- 5) dispatch integration: text mode prepends, skip drops ---------------
bot = {"out": type("Q", (), {"items": [], "put": lambda self, x: self.items.append(x),
                             "qsize": lambda self: len(self.items)})()}
core._bots["nickZ"] = bot
logs = []


def _db(mode):
    return type("DB", (), {
        "list_forward_rules": lambda self=None: [_rule(mode)],
        "seen_forward": lambda *a, **k: True,
        "bump_forward_rule": lambda *a, **k: None,
        "add_forward_log": lambda *a, **k: logs.append((a, k)) or True,
        "find_forward_by_src": lambda self=None, *a, **k: None,
        "msgzilla_lookup": lambda self=None, *a, **k: None,
    })()


def _ev():
    return {"group": "G_A", "msgId": "rp-1", "uidFrom": "u1", "name": "Hà", "isSelf": False,
            "ts": "99", "part": {"kind": "text", "text": "chốt sớm TP1"},
            "text": "chốt sớm TP1", "quote": Q_TEXT}


core.db = _db("skip")
core._bot_dedup.clear()
core._bot_handle_message("nickZ", _ev())
check("skip mode -> reply dropped", len(bot["out"].items) == 0)
check("skip mode -> logged as skip:reply",
      any("skip:reply" in str(a) for a in logs), str(logs[-1:] if logs else []))

bot["out"].items.clear(); logs.clear()
core.db = _db("text")
core._bot_dedup.clear()
core._bot_handle_message("nickZ", _ev())
check("text mode -> reply enqueued", len(bot["out"].items) == 1)
if bot["out"].items:
    it = bot["out"].items[0]
    check("enqueued item has reply_prefix", str(it.get("reply_prefix", "")).startswith("↩"))
    check("enqueued item keeps the body parts",
          [p["kind"] for p in it["parts"]] == ["text"], str(it.get("parts")))

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
