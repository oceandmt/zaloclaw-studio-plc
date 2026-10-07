#!/usr/bin/env python3
"""Smoke test: DB + campaign engine DRY-RUN path (no Zalo login needed)."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))
import db, core

db.init()
# fast test settings — SNAPSHOT first so a smoke run never clobbers the real
# operator caps in the DB (regression: it once left min_gap=1/max_per_hour=1000).
_prev_defaults = db.get_setting("defaults")
db.set_setting("defaults", {"min_gap_seconds": 1, "jitter_seconds": 0, "max_per_hour": 1000, "max_per_day": 1000})

def _restore():
    if _prev_defaults is None:
        with db._LOCK, db._conn() as c:
            c.execute("DELETE FROM settings WHERE key='defaults'")
    else:
        db.set_setting("defaults", _prev_defaults)

n = db.add_contacts([{"phone": "0900000001", "display_name": "Test A"},
                     {"phone": "0900000002", "display_name": "Test B"},
                     {"phone": "0900000003", "display_name": "Test C"}])
print("contacts added:", n)
_test_phones = ["0900000001", "0900000002", "0900000003"]

cid = core.new_campaign(name="smoke", kind="message", account_id="default",
                        body_template="Xin chao {name}", dry_run=1)
print("campaign:", cid)
added = db.add_targets(cid, [{"phone": p} for p in _test_phones])
print("targets added:", added)

r = core.start_campaign(cid)
print("start:", r)
for _ in range(40):
    time.sleep(0.5)
    counts = db.campaign_counts(cid)
    if counts.get("pending", 0) == 0:
        break
print("counts:", db.campaign_counts(cid))
print("events:", [(e["kind"], e["detail"]) for e in db.recent_events(10)])
print("logs:")
for l in core.logs(20):
    print("  ", l)
_restore()
assert db.campaign_counts(cid).get("sent", 0) == 3, "expected 3 dry sends"
db.delete_campaign(cid)
for p in _test_phones:
    db.delete_contact(p)
print("\nSMOKE OK ✅")
