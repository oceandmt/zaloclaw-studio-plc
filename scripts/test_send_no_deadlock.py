#!/usr/bin/env python3
"""Regression: the campaign send path must NOT deadlock.

Root cause (2026-10-03): `_process_job` wrapped `_do_send_job(...)` in
`with _send_lock:` AND `_do_send_job` itself did `with _send_lock: sessiond_client.cmd(...)`.
`_send_lock` is a plain (non-reentrant) `threading.Lock`, so the second acquire
by the SAME thread blocks forever -> the single worker wedges, the campaign stays
'running' with a frozen 'pending' target, and no send is ever issued.

This test drives ONE job through the real `_process_job` in a thread and asserts
it COMPLETES within a few seconds. If the nested acquire reappears, the thread
hangs and the join timeout fails the test.

SAFETY: uses an ISOLATED temp database. It never reads or writes the operator's
live data/zs.db (a prior version forgot this and clobbered live rate settings —
see the guard at the bottom).

Run:  webapp/.venv/bin/python scripts/test_send_no_deadlock.py
"""
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "webapp"))
import core  # noqa: E402
import db  # noqa: E402

# --- ISOLATION: point db at a throw-away sqlite file BEFORE any db call ------
_LIVE_DB = db.DB_PATH
_TMP = Path(tempfile.mkdtemp(prefix="zs-deadlock-test-"))
db.DATA = _TMP
db.DB_PATH = _TMP / "test.db"
assert db.DB_PATH != _LIVE_DB, "refusing to run against the live DB"
db.init()

fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + "  " + name + ((" :: " + extra) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


# --- stubs: no real Zalo traffic -------------------------------------------
calls = []
core.sessiond_client.cmd = lambda acct, obj, timeout=180: (calls.append((acct, obj)) or {"ok": True, "msgId": "TESTID"})
core.is_logged_in = lambda a: True
core.account_enabled = lambda a: True

# generous caps, zero gap, quiet hours OFF so the job goes straight to send
db.set_setting("defaults", {
    "quiet_hours_enabled": False, "quiet_from": "22:00", "quiet_to": "06:00",
    "max_per_hour": 1000, "max_per_day": 10000, "min_gap_seconds": 0, "jitter_seconds": 0,
    "max_friend_per_day": 10000,
})

cid = "c_deadlock_test"
db.create_campaign(cid, name="deadlock test", kind="message", account_id="nick1",
                   thread_id=None, thread_type="user", dry_run=0,
                   body_template="hi {name}",
                   blocks=json.dumps([{"type": "text", "content": "hi {name}"}]))
db.set_campaign_status(cid, "running")
db.add_targets(cid, [{"phone": "0900000000", "zalo_user_id": "<USER_ID_C>"}])
tid = db.pending_targets(cid)[0]["id"]

job = {
    "account_id": "nick1", "campaign_id": cid, "target_id": tid,
    "thread": "<USER_ID_C>", "type": "user", "kind": "message",
    "blocks": [{"type": "text", "content": "hi there"}], "text": "hi there",
    "url": None, "images": [], "file_caption": "",
    "alias_uid": "", "alias_phone": "0900000000", "alias_name": "",
    "dry_run": False,
}

# --- drive the REAL _process_job (which holds _send_lock) in a thread --------
def run():
    core._process_job(job)

t = threading.Thread(target=run, name="deadlock-probe", daemon=True)
t0 = time.time()
t.start()
t.join(timeout=10)

check("_process_job returns (no deadlock) within 10s", not t.is_alive(),
      f"thread still alive after {time.time()-t0:.1f}s -> NESTED _send_lock acquire")
check("sessiond send was actually called", len(calls) >= 1, f"calls={len(calls)}")

row = db.get_target(tid)
check("target marked 'sent'", bool(row) and row["status"] == "sent",
      str(row and row["status"]))

# --- safety: the live DB must be untouched ----------------------------------
check("live DB was NOT modified by this test",
      db.DB_PATH == _TMP / "test.db" and str(_LIVE_DB).endswith("data/zs.db"),
      f"live={_LIVE_DB} used={db.DB_PATH}")

shutil.rmtree(_TMP, ignore_errors=True)

print("\nFAILS: none" if not fails else f"\nFAILS: {fails}")
sys.exit(1 if fails else 0)
