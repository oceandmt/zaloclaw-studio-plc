#!/usr/bin/env python3
"""Regression: db.py must NOT leak connections/file-descriptors.

Root cause (2026-10-03): `with sqlite3.connect(...) as c:` only commits the
transaction — it does NOT close the connection. The old `_conn()` returned a
bare connection used as `with _LOCK, _conn() as c:`, so every query leaked one
connection (2 FDs: the db file + the WAL). The panel hit the 1024-FD limit and
every endpoint raised `sqlite3.OperationalError: unable to open database file`.

This test hammers a read endpoint and asserts the FD count stays flat.

Run:  webapp/.venv/bin/python scripts/test_db_no_fd_leak.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "webapp"))
import db  # noqa: E402

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

print("\nFAILS: none" if not fails else f"\nFAILS: {fails}")
sys.exit(1 if fails else 0)
