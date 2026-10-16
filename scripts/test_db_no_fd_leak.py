#!/usr/bin/env python3
"""Regression: db.py must NOT leak connections/file-descriptors.

Root cause (2026-10-03): `with sqlite3.connect(...) as c:` only commits the
transaction — it does NOT close the connection. The old `_conn()` returned a
bare connection used as `with _LOCK, _conn() as c:`, so every query leaked one
connection (2 FDs: the db file + the WAL). The panel hit the 1024-FD limit and
every endpoint raised `sqlite3.OperationalError: unable to open database file`.

This test hammers a read endpoint and asserts the FD count stays flat.

SAFETY: uses an ISOLATED temp database. It never reads or writes the operator's
live data/zs.db (a prior version called db.init() against the live DB, silently
migrating it — see the isolation guard below).

Run:  webapp/.venv/bin/python scripts/test_db_no_fd_leak.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "webapp"))
import db  # noqa: E402

# --- ISOLATION: point db at a throw-away sqlite file BEFORE any db call ------
_LIVE_DB = db.DB_PATH
_TMP = Path(tempfile.mkdtemp(prefix="zs-fdleak-test-"))
db.DATA = _TMP
db.DB_PATH = _TMP / "test.db"
assert db.DB_PATH != _LIVE_DB, "refusing to run against the live DB"

fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + "  " + name + ((" :: " + extra) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


db.init()


def fds():
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


# warm up (first call may lazily open/close a few things)
for _ in range(5):
    db.list_accounts()
base = fds()

N = 400
for _ in range(N):
    db.list_accounts()
    db.list_campaigns()
after = fds()

check(f"{N}x2 queries leak no FDs (delta={after - base})", after - base <= 10,
      f"base={base} after={after}")

# a write path must also release its connection
for _ in range(100):
    db.log_event("test", "fd-leak-probe")
after_w = fds()
check(f"100x write leak no FDs (delta={after_w - base})", after_w - base <= 10,
      f"base={base} after={after_w}")

# --- safety: the live DB must be untouched ----------------------------------
check("live DB was NOT used by this test",
      db.DB_PATH == _TMP / "test.db" and str(_LIVE_DB).endswith("data/zs.db"),
      f"live={_LIVE_DB} used={db.DB_PATH}")

shutil.rmtree(_TMP, ignore_errors=True)

print("\nFAILS: none" if not fails else f"\nFAILS: {fails}")
sys.exit(1 if fails else 0)
