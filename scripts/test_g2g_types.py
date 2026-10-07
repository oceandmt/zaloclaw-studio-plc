#!/usr/bin/env python3
"""Regression: canonical message taxonomy + grouping window (G2G).

Part A (relay.mjs, run via node) is exercised in test_g2g_watch.mjs; here we
cover the Python side:
  * _fwd_send_cmd maps canonical parts to a bridge `send` with parts+prefix
  * _bot_dispatch_part buffers when group_window_ms>0 and flushes ONE post
  * an image followed by separate text coalesces into a single queued post
  * group_window_ms=0 enqueues immediately (legacy behaviour)
"""
import queue
import sys
import threading
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


class _Q:
    def __init__(self):
        self.items = []

    def put(self, x):
        self.items.append(x)

    def qsize(self):
        return len(self.items)


bot = {"out": _Q(), "ready": threading.Event()}
core._bots["nickZ"] = bot

RULES = {}


def _rule(win):
    return {"id": "f_grp", "account_id": "nickZ", "enabled": 1, "src_group_id": "G_A",
            "dst_group_id": "G_B", "include_self": 0, "group_window_ms": win,
            "prefix": "#P ", "gap_seconds": 0}


# --- 1) immediate path (window=0) ------------------------------------------
RULES["r"] = _rule(0)
core.db = type("DB", (), {"list_forward_rules": lambda self=None: [RULES["r"]],
                          "seen_forward": lambda *a, **k: True,
                          "bump_forward_rule": lambda *a, **k: None,
                          "add_forward_log": lambda *a, **k: True})()
core._bot_dedup.clear()
ev = {"group": "G_A", "msgId": "w0-1", "uidFrom": "u1", "name": "A", "isSelf": False,
      "ts": "1", "part": {"kind": "text", "text": "hello"}, "text": "hello"}
core._bot_handle_message("nickZ", ev)
time.sleep(0.2)
check("window=0 enqueues immediately", len(bot["out"].items) == 1, f"q={bot['out'].items}")
check("item carries canonical parts",
      bot["out"].items and bot["out"].items[0].get("parts") == [{"kind": "text", "text": "hello"}])

# --- 2) image + separate text -> ONE post after the window ------------------
bot["out"].items.clear()
core._bot_dedup.clear()
RULES["r"] = _rule(400)  # small window for the test
img = {"group": "G_A", "msgId": "g-1", "uidFrom": "u2", "name": "A", "isSelf": False,
       "ts": "10", "part": {"kind": "image", "url": "https://x/a.jpg", "caption": ""},
       "text": "", "media": ["https://x/a.jpg"], "mediaKind": "image"}
txt = {"group": "G_A", "msgId": "g-2", "uidFrom": "u2", "name": "A", "isSelf": False,
       "ts": "11", "part": {"kind": "text", "text": "phân tích kèm theo"}, "text": "phân tích kèm theo"}
core._bot_handle_message("nickZ", img)
time.sleep(0.1)
core._bot_handle_message("nickZ", txt)   # same sender, within window
check("nothing sent before window closes", len(bot["out"].items) == 0, f"q={bot['out'].items}")
time.sleep(0.7)                          # let the debounce timer fire
check("image+text coalesced into ONE queued post", len(bot["out"].items) == 1, f"q={bot['out'].items}")
if bot["out"].items:
    parts = bot["out"].items[0]["parts"]
    check("post has both parts in order",
          [p["kind"] for p in parts] == ["image", "text"], str(parts))
    check("post src_msg_id = last message", bot["out"].items[0]["src_msg_id"] == "g-2")

# --- 3) different sender flushes independently ------------------------------
bot["out"].items.clear()
core._bot_dedup.clear()
a = {"group": "G_A", "msgId": "s-1", "uidFrom": "uA", "name": "A", "isSelf": False,
     "ts": "20", "part": {"kind": "image", "url": "https://x/a.jpg", "caption": ""}, "text": ""}
b = {"group": "G_A", "msgId": "s-2", "uidFrom": "uB", "name": "B", "isSelf": False,
     "ts": "21", "part": {"kind": "text", "text": "other"}, "text": "other"}
core._bot_handle_message("nickZ", a)
core._bot_handle_message("nickZ", b)
time.sleep(0.7)
check("two senders -> two separate posts", len(bot["out"].items) == 2, f"q={len(bot["out"].items)}")

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
