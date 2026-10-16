#!/usr/bin/env python3
"""Regression: group-member scan must MERGE, never wipe history.

Root cause (2026-10-16): `db.upsert_members` did `DELETE FROM members WHERE
group_id=?` then re-INSERTed whatever the scan returned. The bridge skips a
chunk silently on error (`catch { /* skip chunk */ }`), so a PARTIAL scan
permanently deleted every member it failed to fetch. The scan also threw away
the profile fields the bridge already had (avatar/gender/zaloName/accountStatus)
and kept no first-seen/last-seen history.

This test drives the REAL db.upsert_members against an ISOLATED temp DB and
asserts:
  * new columns exist (schema + idempotent migration);
  * a second scan does not drop members the first one found;
  * first_seen is preserved, last_seen advances;
  * a blank field never clobbers a known value;
  * source upgrades observed -> roster.

SAFETY: isolated temp DB; never touches the operator's live data/zs.db.

Run:  webapp/.venv/bin/python scripts/test_member_scan_upgrade.py
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "webapp"))
import db  # noqa: E402

# --- ISOLATION: point db at a throw-away sqlite file BEFORE any db call ------
_LIVE_DB = db.DB_PATH
_TMP = Path(tempfile.mkdtemp(prefix="zs-members-test-"))
db.DATA = _TMP
db.DB_PATH = _TMP / "test.db"
assert db.DB_PATH != _LIVE_DB, "refusing to run against the live DB"

fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + "  " + name + ((" :: " + extra) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


GID = "group-test-1"
ACCT = "nick1"

# --- schema -----------------------------------------------------------------
db.init()
with db._conn() as c:
    cols = {r["name"] for r in c.execute("PRAGMA table_info(members)").fetchall()}
for col in ("avatar", "gender", "zalo_name", "account_status", "first_seen", "last_seen", "source"):
    check(f"schema has members.{col}", col in cols, str(sorted(cols)))

# migration is idempotent (init twice adds nothing / raises nothing)
try:
    db.init()
    check("init() is idempotent", True)
except Exception as e:  # pragma: no cover
    check("init() is idempotent", False, str(e))

# --- first scan -------------------------------------------------------------
m1 = [
    {"userId": "u1", "displayName": "An", "zaloName": "an.z", "avatar": "http://cdn/a.jpg",
     "gender": "nam", "globalId": "g1", "isAdmin": True, "accountStatus": 0},
    {"userId": "u2", "displayName": "Bình", "zaloName": "binh", "avatar": "http://cdn/b.jpg",
     "gender": "nữ", "globalId": "g2", "isAdmin": False, "accountStatus": 0},
    {"userId": "u3", "displayName": "Cường", "zaloName": "", "avatar": "",
     "gender": "nam", "globalId": "g3", "isAdmin": False, "accountStatus": 0},
]
n1 = db.upsert_members(ACCT, GID, m1, source="roster")
check("first scan writes 3 members", n1 == 3, f"n={n1}")
rows = {r["user_id"]: r for r in db.list_members(GID)}
check("avatar persisted", rows["u1"]["avatar"] == "http://cdn/a.jpg", rows["u1"]["avatar"])
check("gender persisted", rows["u2"]["gender"] == "nữ", rows["u2"]["gender"])
check("zalo_name persisted", rows["u1"]["zalo_name"] == "an.z", rows["u1"]["zalo_name"])
check("first_seen set on insert", bool(rows["u1"]["first_seen"]))
first_seen_u1 = rows["u1"]["first_seen"]

# --- second, PARTIAL scan: only u1 comes back, and with a blank avatar ------
time.sleep(1.1)  # ensure last_seen can advance by >=1s
m2 = [{"userId": "u1", "displayName": "An", "zaloName": "", "avatar": "",
       "gender": "", "globalId": "", "isAdmin": True, "accountStatus": None}]
n2 = db.upsert_members(ACCT, GID, m2, source="roster")
check("partial scan writes 1", n2 == 1, f"n={n2}")

rows2 = {r["user_id"]: r for r in db.list_members(GID)}
check("!! partial scan did NOT delete u2", "u2" in rows2, str(list(rows2)))
check("!! partial scan did NOT delete u3", "u3" in rows2, str(list(rows2)))
check("total members still 3 after partial scan", len(rows2) == 3, str(len(rows2)))
check("first_seen preserved across scans", rows2["u1"]["first_seen"] == first_seen_u1,
      f"{rows2['u1']['first_seen']} vs {first_seen_u1}")
check("last_seen advanced", rows2["u1"]["last_seen"] > first_seen_u1,
      f"{rows2['u1']['last_seen']} vs {first_seen_u1}")
# blank values must NOT clobber known ones
check("blank avatar did not clobber", rows2["u1"]["avatar"] == "http://cdn/a.jpg", rows2["u1"]["avatar"])
check("blank gender did not clobber", rows2["u1"]["gender"] == "nam", rows2["u1"]["gender"])
check("blank zalo_name did not clobber", rows2["u1"]["zalo_name"] == "an.z", rows2["u1"]["zalo_name"])
check("blank global_id did not clobber", rows2["u1"]["global_id"] == "g1", rows2["u1"]["global_id"])
check("account_status kept when new is null", rows2["u1"]["account_status"] == 0,
      str(rows2["u1"]["account_status"]))

# --- source semantics: observed row upgraded to roster, others untouched ----
db.upsert_members(ACCT, GID, [{"userId": "u9", "displayName": "Khách lạ"}], source="observed")
r9 = {r["user_id"]: r for r in db.list_members(GID)}["u9"]
check("observed row stored with source=observed", r9["source"] == "observed", r9["source"])
# a real scan that now sees u9 upgrades the source, and a later observed write
# must not downgrade an existing roster row
db.upsert_members(ACCT, GID, [{"userId": "u9", "displayName": "Khách lạ"}], source="roster")
r9 = {r["user_id"]: r for r in db.list_members(GID)}["u9"]
check("observed -> roster upgrade", r9["source"] == "roster", r9["source"])
db.upsert_members(ACCT, GID, [{"userId": "u9", "displayName": "Khách lạ"}], source="observed")
r9 = {r["user_id"]: r for r in db.list_members(GID)}["u9"]
check("observed write does not downgrade roster", r9["source"] == "roster", r9["source"])

# --- rows without userId are skipped ----------------------------------------
n_skip = db.upsert_members(ACCT, GID, [{"displayName": "no id"}, {"userId": "u10"}], source="roster")
check("blank userId skipped", n_skip == 1, f"n={n_skip}")

# --- safety -----------------------------------------------------------------
check("live DB was NOT modified by this test",
      db.DB_PATH == _TMP / "test.db" and str(_LIVE_DB).endswith("data/zs.db"),
      f"live={_LIVE_DB} used={db.DB_PATH}")

shutil.rmtree(_TMP, ignore_errors=True)

print("\nFAILS: none" if not fails else f"\nFAILS: {fails}")
sys.exit(1 if fails else 0)
