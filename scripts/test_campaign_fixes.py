#!/usr/bin/env python3
"""Regression tests for the campaign-engine fixes F1-F6.

Fully offline: bridge send is monkeypatched, DRY-RUN on, NO Zalo traffic.

  F1  concurrent start no longer double-enqueues one target (atomic claim)
  F2  add_targets counts only rows actually written
  F3  phone variants collapse to ONE target (canonicalisation)
  F4  cross-campaign "already sent recently" gate (LIVE only)
  F5  a rate/backoff-parked job no longer blocks the worker (no sleep in lock)
  F6  a stranded pending target is re-enqueued so the campaign can finish

Run:  webapp/.venv/bin/python scripts/test_campaign_fixes.py
"""
import queue
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "webapp"))
import core  # noqa: E402
import db  # noqa: E402

db.init()
calls: list[dict] = []
core._bot_cmd = lambda *a, **k: {"ok": True}   # not used here

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails += 1


def fresh_campaign(cid, dry=1, **kw):
    db.delete_campaign(cid)
    db.create_campaign(cid, name=cid, account_id="nick1", kind="message", dry_run=dry,
                       blocks='[{"type":"text","content":"hi {name}"}]', **kw)


def trows(cid):
    return db.targets_for(cid, 10000)


# ---------------------------------------------------------------- F2 + F3
fresh_campaign("c_f23")
n = db.add_targets("c_f23", [{"phone": "0912345678"}, {"phone": "0912345678"},
                             {"phone": "+84912345678"}, {"phone": "84912345678"},
                             {"phone": "0912345678"}, {"phone": "   "}])
rows = trows("c_f23")
check("F2: add_targets returns the WRITTEN count", n == len(rows) and n == 1, f"n={n} rows={len(rows)}")
check("F3: phone variants collapse to ONE canonical target",
      len(rows) == 1 and rows[0]["phone"] == "0912345678",
      f"rows={[r['phone'] for r in rows]}")

# ---------------------------------------------------------------- F1
fresh_campaign("c_f1")
db.add_targets("c_f1", [{"phone": f"09000000{i:02d}"} for i in range(1, 6)])
core._JOBS.queue.clear()
core._queued_targets.clear()
core._busy.clear()
r1 = core.start_campaign("c_f1")
r2 = core.start_campaign("c_f1")       # immediate second click / concurrent start
# The second start must enqueue NOTHING. It may legitimately answer in two ways
# depending on how fast the worker drained the first batch: a resume (queued=0)
# or "no pending targets left" (ok:False, no 'queued' key) — both mean zero
# re-enqueues, so treat a missing key as 0.
check("F1: second start does NOT double-enqueue the same targets",
      r1.get("queued") == 5 and (r2.get("queued") or 0) == 0, f"q1={r1.get('queued')} q2={r2.get('queued')}")


def dry_count(cid, want, timeout=15.0):
    import sqlite3
    end = time.time() + timeout
    n = 0
    while True:
        # anchor to the module's DB (repo-root independent), NOT cwd-relative
        # "./data/zs.db" — a caller running this suite from another directory
        # would otherwise read a different database and see n=0.
        c = sqlite3.connect(db.DB_PATH)
        n = c.execute("SELECT COUNT(*) FROM events WHERE campaign_id=? AND kind='send_dry'",
                      (cid,)).fetchone()[0]
        c.close()
        if n >= want or time.time() > end:
            return n
        time.sleep(0.1)


check("F1: exactly 5 dry sends (no duplicate) for 5 targets",
      dry_count("c_f1", 5) == 5, f"n={dry_count('c_f1', 5)}")
core._JOBS.queue.clear()
core._queued_targets.clear()
core._busy.clear()

# ---------------------------------------------------------------- F5
# _requeue_wait used to SLEEP the worker while holding the send lock. It must
# now return immediately (job handed to the scheduler).
job_park = {"target_id": 999999, "campaign_id": "c_park", "account_id": "nick1"}
t0 = time.time()
core._requeue_wait(job_park, "unit-test", 30.0)
elapsed = time.time() - t0
check("F5: _requeue_wait returns immediately (no blocking sleep in worker)",
      elapsed < 1.0, f"elapsed={elapsed:.2f}s")
with core._delayed_lock:
    core._delayed.clear()
core._next_attempt.clear()

# ---------------------------------------------------------------- F4
# F4 exercises the LIVE preflight, which refuses to start when the account is
# not logged in, then auto-resolves phones over the network. Stub both so this
# suite stays fully offline (a fresh clone has no data/accounts/*.json creds).
_orig_is_logged_in = core.is_logged_in
_orig_resolve_phones = core.resolve_phones
_orig_build_campaign_targets = core.build_campaign_targets
core.is_logged_in = lambda acct=None: True
core.resolve_phones = lambda *a, **k: 0
core.build_campaign_targets = lambda *a, **k: 0
calls.clear()
core._JOBS.queue.clear(); core._queued_targets.clear(); core._busy.clear()
fresh_campaign("c_f4a", dry=0)          # LIVE
# seed as if it was already sent today (bypass the send path entirely)
db.add_targets("c_f4a", [{"phone": "0900000301"}])
with db._LOCK, db._conn() as c:
    c.execute("UPDATE targets SET status='sent', ident=?, updated_at=? WHERE campaign_id='c_f4a'",
              (db.canon_ident("0900000301", None), int(time.time())))
# same person (different spelling) in a NEW live campaign
fresh_campaign("c_f4b", dry=0)
db.add_targets("c_f4b", [{"phone": "+84900000301"}])
core._JOBS.queue.clear()
r = core.start_campaign("c_f4b")
st = {t["status"] for t in trows("c_f4b")}
check("F4: already-sent identity is skipped in a later LIVE campaign",
      r.get("queued") == 0 and "skipped" in st, f"r={r} status={st}")
core.is_logged_in = _orig_is_logged_in
core.resolve_phones = _orig_resolve_phones
core.build_campaign_targets = _orig_build_campaign_targets

# ---------------------------------------------------------------- F6
core._JOBS.queue.clear(); core._queued_targets.clear(); core._busy.clear()
fresh_campaign("c_f6")
db.add_targets("c_f6", [{"phone": "0900000401"}, {"phone": "0900000402"}])
core._queued_targets.add(trows("c_f6")[0]["id"])   # simulate a leaked claim
core._reconcile_queue()                            # clears the stale claim
check("F6a: reconcile clears a stale (unbacked) claim", core.queued_count() == 0)
core.start_campaign("c_f6")
check("F6b: no target stranded - both get sent",
      dry_count("c_f6", 2) == 2, f"n={dry_count('c_f6', 2)}")

# ---------------------------------------------------------------- cleanup
for cid in ("c_f23", "c_f1", "c_f5", "c_f5b", "c_f4a", "c_f4b", "c_f6", "c_park"):
    db.delete_campaign(cid)
core._JOBS.queue.clear(); core._queued_targets.clear(); core._busy.clear(); core._next_attempt.clear()

print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
