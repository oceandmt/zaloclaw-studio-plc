#!/usr/bin/env python3
"""Unit tests for the Autobot forward engine (listen A -> send B).

Fully offline: the bridge send is monkeypatched, DRY-RUN is on, so NO Zalo
traffic happens. Verifies match, dedup, self-suppression, group filtering,
prefixing, rate cap and the "no auto-reply" safety default.

Run:  webapp/.venv/bin/python scripts/test_autobot.py
"""
import queue
import sqlite3
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "webapp"))
import core  # noqa: E402
import db  # noqa: E402

db.init()
RID = "f_utest"
db.delete_forward_rule(RID)
# test hygiene: drop any residue in the shared dedup ledger for our synthetic
# groups (forward_seen has a 24h TTL, so a prior run would mask re-runs)
try:
    _c = sqlite3.connect(str(HERE / "data" / "zs.db"))
    _c.execute("delete from forward_seen where group_id in ('G_A','G_B')")
    _c.execute("delete from forward_log where rule_id like 'f_utest%'")
    _c.commit(); _c.close()
except Exception:
    pass
db.create_forward_rule(RID, name="utest", account_id="nick1", src_group_id="G_A",
                       dst_group_id="G_B", dry_run=1, enabled=1, max_per_hour=100,
                       gap_seconds=0, prefix="[P] ", group_window_ms=0)

calls: list[dict] = []
core._bot_cmd = lambda aid, obj, timeout=25: (calls.append(obj) or {"ok": True, "msgId": "MID"})


class _FakeProc:
    def poll(self):
        return None


class _FakeStdin:
    def write(self, s):
        pass

    def flush(self):
        pass


bot = {"proc": _FakeProc(), "account": "nick1", "started": time.time(),
       "ready": threading.Event(), "error": "", "own_id": "", "out": queue.Queue()}
core._bots["nick1"] = bot
threading.Thread(target=core._bot_sender_loop, args=("nick1",), daemon=True).start()
core._fwd_last.pop(RID, None)

fails = 0


def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


EV = {"event": "msg", "account": "nick1", "group": "G_A", "msgId": "m1",
      "uidFrom": "u1", "name": "An", "isSelf": False, "text": "Xin chào"}

# 1) normal forward (dry -> no real send)
core._bot_handle_message("nick1", EV)
time.sleep(0.8)
check("a matched message is queued to B", len(calls) == 1)
check("sent to destination group B", calls and calls[0]["thread"] == "G_B")
check("prefix applied", calls and (calls[0].get("text") == "[P] Xin chào" or calls[0].get("prefix") == "[P] "))
check("DRY-RUN enforced (dryRun=True)", calls and calls[0]["dryRun"] is True)
check("logged as dry", any(l["status"] == "dry" for l in db.list_forward_log(20, RID)))

# 2) dedup: same msgId not forwarded twice
core._bot_handle_message("nick1", EV)
time.sleep(0.3)
check("duplicate msgId ignored", len(calls) == 1)

# 3) self message suppressed unless include_self
core._bot_handle_message("nick1", {**EV, "msgId": "m2", "isSelf": True})
time.sleep(0.3)
check("self message NOT forwarded (no auto-reply echo)", len(calls) == 1)

# 4) other group ignored
core._bot_handle_message("nick1", {**EV, "msgId": "m3", "group": "G_OTHER"})
time.sleep(0.3)
check("message from another group ignored", len(calls) == 1)

# 5) empty text skipped + logged
core._bot_handle_message("nick1", {**EV, "msgId": "m4", "text": "  "})
time.sleep(0.3)
check("empty message skipped", len(calls) == 1
      and any("rỗng" in (l["status"] or "") for l in db.list_forward_log(20, RID)))

# 6) disabled rule not matched
db.update_forward_rule(RID, enabled=0)
core._bot_handle_message("nick1", {**EV, "msgId": "m5", "text": "while off"})
time.sleep(0.3)
check("disabled rule does not forward", len(calls) == 1)
db.update_forward_rule(RID, enabled=1)

# 7) rate cap
_orig = db.count_forwarded_since
db.count_forwarded_since = lambda rid, since: 10_000
core._bot_handle_message("nick1", {**EV, "msgId": "m6", "text": "capped"})
time.sleep(0.5)
db.count_forwarded_since = _orig
check("over hourly cap -> skipped (not sent)", len(calls) == 1
      and any("hạn/giờ" in (l["status"] or "") for l in db.list_forward_log(20, RID)))

# 8) KILLSWITCH stop
core.KILLSWITCH = HERE / "data" / "KILLSWITCH"
core.set_killswitch(True)
core._bot_handle_message("nick1", {**EV, "msgId": "m7", "text": "killed"})
time.sleep(0.5)
core.set_killswitch(False)
check("killswitch blocks the send", len(calls) == 1
      and any("killswitch" in (l["status"] or "") for l in db.list_forward_log(20, RID)))

# cleanup
bot["out"].put(None)
core._bots.pop("nick1", None)
db.delete_forward_rule(RID)

print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
