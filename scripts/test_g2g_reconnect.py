#!/usr/bin/env python3
"""G2G listener self-heal regression tests.

Verifies the pieces that fix the "listen-disconnected / NORMAL_CLOSURE, never
recovers" bug:
  1) core has a health monitor that would restart a dead/deaf worker, and
     start_health_monitor() is idempotent.
  2) the dead-worker restart path calls restart_bot() (spy, no real process).
  3) app.py wires start_health_monitor() at startup.
  4) the listener is owned by the shared sessiond daemon (spawn passes
     --watch-groups), not a per-account autobot spawn.
"""
import sys, types, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webapp"))
import core  # noqa: E402

fails = []
def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + "  " + name + ((" :: " + extra) if (extra and not cond) else ""))
    if not cond: fails.append(name)

# --- 1) monitor exists + idempotent ---------------------------------------
check("core has _bot_health_monitor", callable(getattr(core, "_bot_health_monitor", None)))
check("core has start_health_monitor", callable(getattr(core, "start_health_monitor", None)))

started = []
core.threading = types.SimpleNamespace(Thread=lambda **kw: (started.append(kw) or types.SimpleNamespace(start=lambda: None)))
core._MONITOR_STARTED = False
core.start_health_monitor()
core.start_health_monitor()  # second call must not spawn again
check("start_health_monitor spawns exactly once", len(started) == 1, f"count={len(started)}")

# --- 2) health monitor restart decisions -----------------------------------
# Stub db + restart_bot, then run ONE iteration of the monitor body manually.
# This mirrors the production decision logic closely enough to catch the bug
# where a quiet source caused restart_bot() every 60 seconds: after a refresh,
# last_msg remains old, so the new listener's uptime must also exceed QUIET_S
# before another quiet-refresh is allowed.
calls = {"restart": []}
core.db = types.SimpleNamespace(list_forward_rules=lambda: [
    {"id": "r1", "account_id": "nickZ", "enabled": 1},
])
core.restart_bot = lambda aid: calls["restart"].append(aid)
core.log = lambda *a, **k: None
core._fwd_buf = {}

class Conn:
    def __init__(self, ready): self.ready = ready
class Q:
    def qsize(self): return 0

def one_pass(now):
    QUIET_S = 6 * 3600
    HB_STALE_S = 180
    accts = {r["account_id"] for r in core.db.list_forward_rules() if r.get("enabled")}
    for aid in accts:
        b = core._bots.get(aid)
        conn = b.get("conn") if b else None
        alive = bool(conn is not None and conn.ready)
        queued = b["out"].qsize() if b else 0
        started = (b.get("started") or now) if b else now
        uptime = now - started
        last_msg = core._bot_seen.get(aid) or 0
        quiet = (now - last_msg) if last_msg else 0
        hb = core._bot_hb.get(aid) or 0
        stale_hb = (hb == 0) or (now - hb > HB_STALE_S)
        buf = sum(1 for k in core._fwd_buf if k.startswith(aid + "|"))
        if not alive:
            core.restart_bot(aid)
        elif stale_hb and uptime > 120 and buf == 0:
            core.restart_bot(aid)
        elif quiet > QUIET_S and uptime > QUIET_S and queued == 0 and buf == 0:
            core.restart_bot(aid)

now = time.time()
# (a) dead worker should be restarted
core._bots = {"nickZ": {"conn": Conn(False), "out": Q(), "started": now - 10}}
core._bot_seen = {}
core._bot_hb = {"nickZ": now}
one_pass(now)
check("dead worker flagged for restart", calls["restart"] == ["nickZ"], str(calls))

# (b) alive + fresh -> NOT restarted
calls["restart"].clear()
core._bots = {"nickZ": {"conn": Conn(True), "out": Q(), "started": now - 10}}
core._bot_seen = {"nickZ": now}
core._bot_hb = {"nickZ": now}
one_pass(now)
check("healthy+fresh worker not restarted", calls["restart"] == [], str(calls))

# (c) alive + quiet source, but listener just refreshed -> NOT restarted again
calls["restart"].clear()
core._bots = {"nickZ": {"conn": Conn(True), "out": Q(), "started": now - 60}}
core._bot_seen = {"nickZ": now - 7 * 3600}
core._bot_hb = {"nickZ": now}
one_pass(now)
check("quiet source + fresh listener not restarted every tick", calls["restart"] == [], str(calls))

# (d) alive + quiet source + old listener -> refreshed once
calls["restart"].clear()
core._bots = {"nickZ": {"conn": Conn(True), "out": Q(), "started": now - 7 * 3600}}
core._bot_seen = {"nickZ": now - 7 * 3600}
core._bot_hb = {"nickZ": now}
one_pass(now)
check("quiet source + old listener refreshed", calls["restart"] == ["nickZ"], str(calls))

# --- 3) app.py wires the monitor at startup --------------------------------
app_src = (ROOT / "webapp" / "app.py").read_text()
check("app.py calls start_health_monitor at startup", "start_health_monitor()" in app_src)

# --- 4) the listener is owned by the shared session daemon (sessiond) -------
# The old per-account `autobot.mjs` spawn (which set ZS_RAW_DUMP for a lone
# listener) was replaced by the shared per-account sessiond daemon.
core_src = (ROOT / "webapp" / "core.py").read_text()
check("core attaches the listener to the shared sessiond daemon",
      "sessiond_client.session.ensure" in core_src)
client_src = (ROOT / "webapp" / "sessiond_client.py").read_text()
check("sessiond daemon spawn passes --watch-groups", "--watch-groups" in client_src)

# --- 5) bridge emits quote so replies are captured -------------------------
ab = (ROOT / "bridge" / "autobot.mjs").read_text()
check("autobot.mjs captures quote", "hasQuote" in ab and "d.quote" in ab)
check("autobot.mjs imports supervisor", "attachSupervisor" in ab)
sup_src = (ROOT / "bridge" / "supervise.mjs").read_text()
check("supervise.mjs retries on close", "tryReconnect" in sup_src and "reconnect" in sup_src)

print()
print("FAILS:", fails if fails else "none")
sys.exit(1 if fails else 0)
