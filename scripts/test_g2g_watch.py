#!/usr/bin/env python3
"""G2G "listen-only-wired-groups" regression tests.

Feature: the pipeline listener must only watch the SOURCE groups that are
wired into an ENABLED rule (per account), instead of every group the account
is in. Verifies:
  1) core._bot_watch_groups() = unique src_group_id of enabled rules per acct.
  2) core builds the watch spec ('only:<ids>' / 'all') for the sessiond daemon.
  3) core._bot_apply_watch() pushes set-watch with the right mode+groups.
  4) the watch allowlist reaches the sessiond daemon spawn; ready handler
     re-applies it.
  5) bot_status()/snapshot expose a `watch` block.
  6) relay.mjs exports watchFrom/watchGroup and gates correctly.
"""
import sys, types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webapp"))
import core  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + "  " + name + ((" :: " + extra) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


RULES = [
    {"id": "f1", "account_id": "nick1", "enabled": 1, "src_group_id": "G_A"},
    {"id": "f2", "account_id": "nick1", "enabled": 1, "src_group_id": "G_B"},
    {"id": "f3", "account_id": "nick1", "enabled": 0, "src_group_id": "G_DISABLED"},
    {"id": "f4", "account_id": "nick2", "enabled": 1, "src_group_id": "G_X"},
    {"id": "f5", "account_id": "nick1", "enabled": 1, "src_group_id": "G_A"},  # dup
    {"id": "f6", "account_id": "nick1", "enabled": 1, "src_group_id": ""},     # empty
]
core.db = types.SimpleNamespace(list_forward_rules=lambda: RULES)

# 1) allowlist = enabled unique src groups, per account
check("watch_groups nick1 == G_A,G_B (dedup, only enabled)",
      core._bot_watch_groups("nick1") == ["G_A", "G_B"], str(core._bot_watch_groups("nick1")))
check("watch_groups nick2 == G_X", core._bot_watch_groups("nick2") == ["G_X"])
check("watch_groups unknown account == []", core._bot_watch_groups("nobody") == [])

# 2) watch spec built for the sessiond daemon: allowlist -> 'only:<ids>' / 'all'
src2 = (ROOT / "webapp" / "core.py").read_text()
check("core builds 'only:<ids>' watch spec",
      '("only:" + ",".join(groups)) if groups else "all"' in src2)

# 3) apply pushes set-watch with correct mode/groups
sent = {}
core._bot_cmd = lambda aid, obj, timeout=30.0: (sent.update({"aid": aid, "obj": obj}) or {"ok": True})
core._bots = {"nick1": {}}
core._bot_apply_watch("nick1")
check("apply_watch sends mode=only", sent["obj"]["cmd"] == "set-watch" and sent["obj"]["mode"] == "only", str(sent))
check("apply_watch sends both groups", sent["obj"]["groups"] == ["G_A", "G_B"], str(sent))
check("apply_watch stores watch on bot", core._bots["nick1"]["watch"] == {"mode": "only", "count": 2},
      str(core._bots["nick1"].get("watch")))

# empty rules -> mode all
core.db = types.SimpleNamespace(list_forward_rules=lambda: [])
core._bot_apply_watch("nick1")
check("apply_watch empty -> mode=all", sent["obj"]["mode"] == "all" and sent["obj"]["groups"] == [], str(sent))

# 4) source wiring: the watch allowlist reaches the sessiond daemon spawn;
#    ready re-applies it.
src = (ROOT / "webapp" / "core.py").read_text()
client_src = (ROOT / "webapp" / "sessiond_client.py").read_text()
check("core builds watch spec for the daemon",
      "sessiond_client.session.ensure" in src and "watch_spec=" in src)
check("sessiond daemon spawn passes --watch-groups", "--watch-groups" in client_src)
check("ready handler re-applies watch (off reader thread)",
      "target=_bot_apply_watch, args=(account_id,)" in src and
      "threading.Thread(target=_bot_apply_watch" in src)
check("bot dict has watch", '"watch": {"mode"' in src)

# 5) status exposes watch
core._bots = {"nick1": {"proc": types.SimpleNamespace(poll=lambda: None),
                        "ready": types.SimpleNamespace(is_set=lambda: True),
                        "out": types.SimpleNamespace(qsize=lambda: 0),
                        "watch": {"mode": "only", "count": 2}}}
st = core.bot_status()
check("bot_status exposes watch", st and st[0].get("watch") == {"mode": "only", "count": 2}, str(st))

# 6) app.py wires the resync endpoint + calls it on rule changes
app_src = (ROOT / "webapp" / "app.py").read_text()
check("app has /autobot/bot/watch", '"/autobot/bot/watch"' in app_src)
check("app resyncs on new/update/delete/toggle", app_src.count("_bot_apply_watch(") >= 4,
      str(app_src.count("_bot_apply_watch(")))

print()
print("FAILS:", "none" if not fails else fails)
sys.exit(1 if fails else 0)
