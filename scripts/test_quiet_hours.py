#!/usr/bin/env python3
"""Regression: quiet-hours (giờ im lặng) window logic.

The operator can pick a daily window during which the send engine emits NO live
message/friend sends. Times are local "HH:MM"; the window may cross midnight.

Pure-logic only (no DB, no bridge): we inject a synthetic `now` so the tests are
deterministic regardless of wall-clock time.

Run:  webapp/.venv/bin/python scripts/test_quiet_hours.py
"""
import sys
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


def at(hh, mm, day_offset=0):
    """Local epoch for today+day_offset at HH:MM (DST-safe)."""
    b = time.localtime()
    return time.mktime(time.struct_time((b.tm_year, b.tm_mon, b.tm_mday + day_offset, hh, mm, 0, 0, 0, -1)))


# --- 1) parser --------------------------------------------------------------
check("parse '22:00'", core._hhmm_to_min("22:00") == 1320)
check("parse '6:05'", core._hhmm_to_min("6:05") == 365)
check("parse '0600'", core._hhmm_to_min("0600") == 360)
check("parse '00:00'", core._hhmm_to_min("00:00") == 0)
check("parse '24:00' -> None", core._hhmm_to_min("24:00") is None)
check("parse '22:99' -> None", core._hhmm_to_min("22:99") is None)
check("parse '' -> None", core._hhmm_to_min("") is None)
check("roundtrip 1320 -> '22:00'", core._min_to_hhmm(1320) == "22:00")
check("roundtrip 0 -> '00:00'", core._min_to_hhmm(0) == "00:00")

# --- 2) window arming -------------------------------------------------------
check("disabled -> None", core.quiet_window({"quiet_hours_enabled": False,
                                             "quiet_from": "22:00", "quiet_to": "06:00"}) is None)
check("equal endpoints -> None (never 24/7 silence)",
      core.quiet_window({"quiet_hours_enabled": True, "quiet_from": "08:00", "quiet_to": "08:00"}) is None)
check("invalid -> None", core.quiet_window({"quiet_hours_enabled": True,
                                           "quiet_from": "zz", "quiet_to": "06:00"}) is None)
check("valid -> (a,b)",
      core.quiet_window({"quiet_hours_enabled": True, "quiet_from": "22:00", "quiet_to": "06:00"}) == (1320, 360))

# --- 3) same-day window 09:00-17:00 ----------------------------------------
day = {"quiet_hours_enabled": True, "quiet_from": "09:00", "quiet_to": "17:00"}
check("09:00-17:00 @ 08:00 -> outside", not core.in_quiet_hours(day, at(8, 0)))
check("09:00-17:00 @ 10:00 -> inside", core.in_quiet_hours(day, at(10, 0)))
check("09:00-17:00 @ 12:30 -> inside", core.in_quiet_hours(day, at(12, 30)))
check("09:00-17:00 @ 16:59 -> inside", core.in_quiet_hours(day, at(16, 59)))
check("09:00-17:00 @ 17:00 -> outside (end exclusive)", not core.in_quiet_hours(day, at(17, 0)))
check("09:00-17:00 @ 09:00 -> outside (start exclusive)", not core.in_quiet_hours(day, at(9, 0)))
check("09:00-17:00 @ 20:00 -> outside", not core.in_quiet_hours(day, at(20, 0)))

# --- 4) crossing-midnight window 22:00-06:00 -------------------------------
night = {"quiet_hours_enabled": True, "quiet_from": "22:00", "quiet_to": "06:00"}
check("22:00-06:00 @ 23:00 -> inside", core.in_quiet_hours(night, at(23, 0)))
check("22:00-06:00 @ 00:30 -> inside", core.in_quiet_hours(night, at(0, 30)))
check("22:00-06:00 @ 05:59 -> inside", core.in_quiet_hours(night, at(5, 59)))
check("22:00-06:00 @ 06:00 -> outside", not core.in_quiet_hours(night, at(6, 0)))
check("22:00-06:00 @ 12:00 -> outside", not core.in_quiet_hours(night, at(12, 0)))
check("22:00-06:00 @ 21:59 -> outside", not core.in_quiet_hours(night, at(21, 59)))

# --- 5) seconds-left --------------------------------------------------------
left = core.quiet_seconds_left(night, at(23, 0))
check("23:00 in 22-06 -> ~7h left (7*3600)", left == 7 * 3600, str(left))
check("12:00 not quiet -> 0 left", core.quiet_seconds_left(night, at(12, 0)) == 0)
check("left is a whole number of minutes", left % 60 == 0)

# --- 6) quiet_status() shape ------------------------------------------------
st = core.quiet_status(night | {"quiet_from": "22:00", "quiet_to": "06:00"})
check("status has keys", {"enabled", "from", "to", "active", "until", "left"} <= set(st))
st_off = core.quiet_status({"quiet_hours_enabled": False})
check("status disabled -> not active", st_off["enabled"] is False and st_off["active"] is False)

print("\nFAILS: none" if not fails else f"\nFAILS: {fails}")
sys.exit(1 if fails else 0)
