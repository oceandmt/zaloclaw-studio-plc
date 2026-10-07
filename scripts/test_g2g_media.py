#!/usr/bin/env python3
"""G2G media-forward regression: photo+text messages must relay the IMAGE.

Covers: _fwd_send_cmd (blocks vs text), _bot_handle_message (media capture),
_bot_enqueue (media carried on the queue), and classify/pick logic mirrors.
"""
import sys, types, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))

import core  # noqa: E402

fails = []
def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + "  " + name + ((" :: " + extra) if (extra and not cond) else ""))
    if not cond: fails.append(name)

# ---------- 1) _fwd_send_cmd ----------
def parts_of(cmd):
    return cmd.get("parts") or []

c1 = core._fwd_send_cmd("G_B", "xin chao",
                        [{"kind": "image", "url": "https://a/x.jpg", "caption": "xin chao"}],
                        "", True)
check("text-only uses text path (no parts)",
      core._fwd_send_cmd("G_B", "hello", [], "", False).get("text") == "hello"
      and "parts" not in core._fwd_send_cmd("G_B", "hello", [], "", False))
check("photo+text -> parts[image] carried to bridge",
      parts_of(c1) == [{"kind": "image", "url": "https://a/x.jpg", "caption": "xin chao"}],
      json.dumps(c1))
check("photo-only keeps image part",
      parts_of(core._fwd_send_cmd("G_B", "", [{"kind": "image", "url": "https://a/y.png", "caption": ""}], "", False))[0]["kind"] == "image")
check("dry flag propagated", c1.get("dryRun") is True and c1.get("thread") == "G_B" and c1.get("type") == "group")
check("prefix attached as separate field",
      core._fwd_send_cmd("G_B", "t", [{"kind": "text", "text": "t"}], "[PFX] ", True).get("prefix") == "[PFX] ")
check("multi-photo album carried as one part urls",
      core._fwd_send_cmd("G_B", "cap",
          [{"kind": "image", "url": "https://a/1.jpg", "caption": "cap"},
           {"kind": "image", "url": "https://a/2.jpg", "caption": ""}], "", False)["parts"][0]["kind"] == "image")

# ---------- 2) event pipeline: capture media + enqueue ----------
class FakeRule(dict): pass
captured = {}

class FakeBot:
    def __init__(self): self.out = _Q()
class _Q:
    def __init__(self): self.items = []
    def put(self, x): self.items.append(x)
    def get(self): return self.items.pop(0) if self.items else None
    def qsize(self): return len(self.items)

core._bots["nickX"] = {"proc": types.SimpleNamespace(poll=lambda: None), "out": _Q(),
                        "ready": types.SimpleNamespace(is_set=lambda: True), "own_id": "", "error": ""}

# stub db calls
core.db = types.SimpleNamespace(
    list_forward_rules=lambda: [{"id": "f_test", "account_id": "nickX", "enabled": 1,
        "src_group_id": "G_A", "dst_group_id": "G_B", "include_self": 0,
        "group_window_ms": 0}],
    get_forward_rule=lambda rid: {"id": "f_test", "account_id": "nickX", "enabled": 1},
    seen_forward=lambda *a, **k: True,
    bump_forward_rule=lambda *a, **k: None,
    add_forward_log=lambda *a, **k: True,
)

ev_photo = {"group": "G_A", "msgId": "111222", "uidFrom": "u1", "name": "Tin",
            "isSelf": False, "ts": "123", "msgType": "chat.photo",
            "mediaKind": "image", "media": ["https://cdn/photo.jpg"],
            "text": "Giá thép hôm nay"}
core._bot_handle_message("nickX", ev_photo)
q = core._bots["nickX"]["out"].items
check("photo event enqueued (not skipped)", len(q) == 1, f"q={q}")
if q:
    it = q[0]
    check("enqueued media carried", it.get("media") == ["https://cdn/photo.jpg"], json.dumps(it))
    check("enqueued text carried", it.get("text") == "Giá thép hôm nay")
    check("enqueued dst group", it.get("dst_group_id") == "G_B")

# photo WITHOUT caption still relays (regression: was skipped as empty)
core._bots["nickX"]["out"].items.clear()
core._bot_handle_message("nickX", {"group": "G_A", "msgId": "333", "uidFrom": "u1",
    "name": "Tin", "isSelf": False, "ts": "9", "msgType": "chat.photo",
    "mediaKind": "image", "media": ["https://cdn/nocap.jpg"], "text": ""})
check("photo without caption still enqueued", len(core._bots["nickX"]["out"].items) == 1)

# truly empty still skipped
core._bots["nickX"]["out"].items.clear()
core._bot_handle_message("nickX", {"group": "G_A", "msgId": "444", "uidFrom": "u1",
    "name": "Tin", "isSelf": False, "ts": "9", "msgType": "webchat", "media": [], "text": ""})
check("empty text+no media skipped", len(core._bots["nickX"]["out"].items) == 0)

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
