#!/usr/bin/env python3
"""Leave Zalo groups listed in a CSV (columns: stt,group_id,ten_nhom,tong_thanh_vien).

Attaches to the EXISTING per-account sessiond daemon (never logs in a second
session). Dry-run by default; pass --live to actually leave.

Safety:
  * PROTECTED group ids (pipeline sources/dests) are refused even if present.
  * Paced with jitter to reduce Zalo anti-spam lock risk.
  * Resumable: a progress JSONL lets a re-run skip ids already left.

Usage:
  webapp/.venv/bin/python scripts/leave_groups.py ~/apollo_groups.csv            # dry-run
  webapp/.venv/bin/python scripts/leave_groups.py ~/apollo_groups.csv --live     # REAL
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webapp"))
import sessiond_client  # noqa: E402

# Never leave these: G2G/S01 pipeline source+dest groups.
PROTECTED = {
    "<DST_GROUP_ID>",   # S01 source A (<NHOM_NGUON_B>)
    "<S01_DST_GROUP_ID>",   # S01 dest B (test automation)
    "<SRC_GROUP_ID>",   # legacy G2G group
}
ACCOUNT = "nick1"
PROGRESS = ROOT / "data" / "leave_groups_progress.jsonl"


def load_csv(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            gid = (r.get("group_id") or "").strip()
            if gid:
                rows.append({"group_id": gid, "name": (r.get("ten_nhom") or "").strip()})
    return rows


def load_done() -> set[str]:
    done: set[str] = set()
    if PROGRESS.exists():
        for line in PROGRESS.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
                if o.get("ok") and not o.get("dryRun"):
                    done.add(o["group_id"])
            except Exception:
                pass
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--live", action="store_true", help="actually leave (default: dry-run)")
    ap.add_argument("--min-gap", type=float, default=4.0)
    ap.add_argument("--max-gap", type=float, default=9.0)
    ap.add_argument("--account", default=ACCOUNT)
    args = ap.parse_args()

    rows = load_csv(args.csv)
    done = load_done()
    todo = [r for r in rows if r["group_id"] not in done and r["group_id"] not in PROTECTED]
    blocked = [r for r in rows if r["group_id"] in PROTECTED]

    live = args.live
    print(f"CSV: {args.csv}")
    print(f"total={len(rows)}  already_left={len(done)}  protected_skipped={len(blocked)}  todo={len(todo)}")
    print(f"MODE: {'LIVE (real leaves)' if live else 'DRY-RUN (no changes)'}")
    for b in blocked:
        print(f"  PROTECTED (skip): {b['group_id']} {b['name']}")
    if not todo:
        print("nothing to do")
        return 0

    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    pf = PROGRESS.open("a", encoding="utf-8")
    ok = err = 0
    for i, r in enumerate(todo, 1):
        gid, name = r["group_id"], r["name"]
        res = sessiond_client.cmd(args.account, {"cmd": "leave-group", "groupId": gid, "dryRun": (not live)}, timeout=60)
        good = bool(res.get("ok")) and not res.get("error")
        if good:
            ok += 1
        else:
            err += 1
        stamp = int(time.time())
        pf.write(json.dumps({"ts": stamp, "group_id": gid, "name": name, "ok": good,
                             "dryRun": (not live), "resp": res}, ensure_ascii=False) + "\n")
        pf.flush()
        print(f"[{i}/{len(todo)}] {'OK ' if good else 'ERR'} {gid} {name[:40]} :: {json.dumps(res, ensure_ascii=False)[:120]}")
        if live and i < len(todo):
            time.sleep(random.uniform(args.min_gap, args.max_gap))
    pf.close()
    print(f"\nDONE ok={ok} err={err} mode={'LIVE' if live else 'DRY-RUN'}")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
