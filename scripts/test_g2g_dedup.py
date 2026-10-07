#!/usr/bin/env python3
"""Regression tests for the G2G dedup fixes (3 weaknesses).

Fully offline: bridge send is monkeypatched, DRY-RUN on, NO Zalo traffic.

Covers:
  L1  dedup survives an in-memory cache reset (restart / replay)  -> durable ledger
  L2  forward_log has a DB-level unique guard on (rule, group, src_msg_id)
  L3  two identical events with EMPTY msgId are deduped (content hash fallback)
  L4  realMsgId is used as the key when msgId is absent

Run:  webapp/.venv/bin/python scripts/test_g2g_dedup.py
"""
import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "webapp"))
import core  # noqa: E402
import db  # noqa: E402

db.init()
RID = "f_g2gd"
db.delete_forward_rule(RID)
db.create_forward_rule(RID, name="g2gd", account_id="nick1", src_group_id="G_A",
                       dst_group_id="G_B", dry_run=1, enabled=1, max_per_hour=1000,
                       gap_seconds=0, prefix="", group_window_ms=0)

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


def reset_state():
    """Simulate a panel restart: wipe in-memory dedup + queue + logs."""
    core._bot_dedup.clear()
    core._fwd_last.pop(RID, None)
    while not bot["out"].empty():
        try:
            bot["out"].get_nowait()
        except Exception:
            break
    with db._LOCK, db._conn() as c:
        c.execute("DELETE FROM forward_log WHERE rule_id=?", (RID,))
        c.execute("DELETE FROM forward_seen WHERE account_id=?", ("nick1",))
        c.execute("DELETE FROM forward_log WHERE rule_id=?", (RID,))
    calls.clear()


EV = {"event": "msg", "account": "nick1", "group": "G_A", "msgId": "m1",
      "uidFrom": "u1", "name": "An", "isSelf": False, "text": "Xin chào"}

# --- baseline: a matched message is forwarded once -------------------------
reset_state()
core._bot_handle_message("nick1", EV)
time.sleep(0.6)
check("baseline: message forwarded once", len(calls) == 1)

# --- L1: dedup survives a restart / replay ---------------------------------
core._bot_handle_message("nick1", EV)
time.sleep(0.3)
check("L1a: same id in-process deduped", len(calls) == 1)

core._bot_dedup.clear()          # simulate restart, keep durable ledger
core._fwd_last.pop(RID, None)
core._bot_handle_message("nick1", EV)   # replay on reconnect
time.sleep(0.4)
check("L1b: replay after restart NOT re-forwarded (durable ledger)", len(calls) == 1)

# --- L2: forward_log DB unique guard ---------------------------------------
reset_state()
txt = "unique-guard"
db.add_forward_log(RID, "G_A", "An", txt, "dry", src_msg_id="uX")
first = db.add_forward_log(RID, "G_A", "An", txt, "dry", src_msg_id="uX")
with db._LOCK, db._conn() as c:
    n = c.execute("SELECT COUNT(*) FROM forward_log WHERE rule_id=? AND src_msg_id='uX'",
                  (RID,)).fetchone()[0]
check("L2a: duplicate log row rejected by unique index", first is False and n == 1)

# --- L3: empty msgId -> content-hash fallback ------------------------------
reset_state()
EV_no_id = {"event": "msg", "account": "nick1", "group": "G_A", "msgId": "",
            "realMsgId": "", "uidFrom": "u9", "ts": "111", "name": "Bo",
            "isSelf": False, "text": "khong co id"}
core._bot_handle_message("nick1", dict(EV_no_id))
time.sleep(0.5)
core._bot_handle_message("nick1", dict(EV_no_id))   # identical, still no id
time.sleep(0.4)
check("L3: identical no-msgId events forwarded only once (hash fallback)", len(calls) == 1)

# --- L4: realMsgId used when msgId is absent -------------------------------
reset_state()
EV_real = {"event": "msg", "account": "nick1", "group": "G_A", "msgId": "",
           "realMsgId": "RID-42", "uidFrom": "u7", "ts": "222", "name": "Cy",
           "isSelf": False, "text": "co realMsgId"}
core._bot_handle_message("nick1", dict(EV_real))
time.sleep(0.5)
core._bot_handle_message("nick1", {**EV_real, "ts": "999"})  # different ts, same realMsgId
time.sleep(0.4)
check("L4: realMsgId key dedups across differing ts", len(calls) == 1)

# --- cleanup ---------------------------------------------------------------
bot["out"].put(None)
core._bots.pop("nick1", None)
db.delete_forward_rule(RID)
with db._LOCK, db._conn() as c:
    c.execute("DELETE FROM forward_seen WHERE account_id=?", ("nick1",))
    c.execute("DELETE FROM forward_log WHERE rule_id=?", (RID,))

print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
