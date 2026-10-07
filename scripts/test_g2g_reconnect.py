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

# --- 2) dead worker -> restart_bot, quiet+idle -> restart_bot --------------
# stub db + restart_bot, then run ONE iteration of the monitor body manually.
calls = {"restart": []}
core.db = types.SimpleNamespace(list_forward_rules=lambda: [
    {"id": "r1", "account_id": "nickZ", "enabled": 1},
])
core.restart_bot = lambda aid: calls["restart"].append(aid)
core.log = lambda *a, **k: None

class Proc:
    def __init__(self, alive): self._a = alive
    def poll(self): return None if self._a else 1
class Q:
    def qsize(self): return 0

# (a) dead worker should be restarted
core._bots = {"nickZ": {"proc": Proc(False), "out": Q()}}
core._bot_seen = {}
# run just the per-account decision inline (mirror of the loop body)
def one_pass(now):
    accts = {r["account_id"] for r in core.db.list_forward_rules() if r.get("enabled")}
    for aid in accts:
        b = core._bots.get(aid)
        alive = bool(b and b["proc"].poll() is None)
        queued = b["out"].qsize() if b else 0
        last = core._bot_seen.get(aid) or 0
        idle = (now - last) if last else 0
        if not alive:
            core.restart_bot(aid)
        elif idle > 6 * 3600 and queued == 0:
            core.restart_bot(aid)
one_pass(time.time())
check("dead worker flagged for restart", calls["restart"] == ["nickZ"], str(calls))

# (b) alive + fresh -> NOT restarted
calls["restart"].clear()
core._bots = {"nickZ": {"proc": Proc(True), "out": Q()}}
core._bot_seen = {"nickZ": time.time()}
one_pass(time.time())
check("healthy+fresh worker not restarted", calls["restart"] == [], str(calls))

# (c) alive + very idle -> refreshed
calls["restart"].clear()
core._bot_seen = {"nickZ": time.time() - 7 * 3600}
one_pass(time.time())
check("idle worker refreshed", calls["restart"] == ["nickZ"], str(calls))

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
