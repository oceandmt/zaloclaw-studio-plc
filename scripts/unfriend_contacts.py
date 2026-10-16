#!/usr/bin/env python3
"""Remove friends listed in a CSV (columns: match,user_id,display_name,zalo_name,phone).

Attaches to the EXISTING per-account sessiond daemon (never logs in a second
session). Dry-run by default; pass --live to actually remove.

Safety:
  * --exclude <user_id> (repeatable) and --exclude-name <substr> skip contacts.
  * Paced with jitter to reduce Zalo anti-spam lock risk.
  * Resumable via progress JSONL (only counts non-dry-run successes).

Usage:
  webapp/.venv/bin/python scripts/unfriend_contacts.py ~/zalo_contacts_apollo_nh.csv \
      --exclude <USER_ID_D>              # dry-run (skips "<TEN_KHACH_F>")
  webapp/.venv/bin/python scripts/unfriend_contacts.py ~/zalo_contacts_apollo_nh.csv \
      --exclude <USER_ID_D> --live       # REAL
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

ACCOUNT = "nick1"
PROGRESS = ROOT / "data" / "unfriend_progress.jsonl"


def load_csv(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            uid = (r.get("user_id") or "").strip()
            if uid:
                rows.append({"user_id": uid,
                             "name": (r.get("display_name") or "").strip(),
                             "zalo": (r.get("zalo_name") or "").strip(),
                             "match": (r.get("match") or "").strip()})
    return rows


def load_done() -> set[str]:
    done: set[str] = set()
    if PROGRESS.exists():
        for line in PROGRESS.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
                if o.get("ok") and not o.get("dryRun"):
                    done.add(o["user_id"])
            except Exception:
                pass
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--live", action="store_true", help="actually remove (default: dry-run)")
    ap.add_argument("--exclude", action="append", default=[], help="user_id to skip (repeatable)")
    ap.add_argument("--exclude-name", action="append", default=[], help="substring of name to skip")
    ap.add_argument("--min-gap", type=float, default=4.0)
    ap.add_argument("--max-gap", type=float, default=9.0)
    ap.add_argument("--account", default=ACCOUNT)
    args = ap.parse_args()

    rows = load_csv(args.csv)
    excl_ids = {e.strip() for e in args.exclude if e.strip()}
    excl_names = [n.strip().lower() for n in args.exclude_name if n.strip()]

    def skip(r: dict) -> str | None:
        if r["user_id"] in excl_ids:
            return "excluded-id"
        hay = f"{r['name']} {r['zalo']}".lower()
        for n in excl_names:
            if n in hay:
                return f"excluded-name:{n}"
        return None

    done = load_done()
    todo, skipped = [], []
    for r in rows:
        why = skip(r)
        if why:
            skipped.append((r, why)); continue
        if r["user_id"] in done:
            skipped.append((r, "already-done")); continue
        todo.append(r)

    live = args.live
    print(f"CSV: {args.csv}")
    print(f"total={len(rows)}  skipped={len(skipped)}  todo={len(todo)}")
    print(f"MODE: {'LIVE (real unfriend)' if live else 'DRY-RUN (no changes)'}")
    for r, why in skipped:
        print(f"  SKIP [{why}] {r['user_id']} {r['name'][:45]}")
    if not todo:
        print("nothing to do"); return 0

    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    pf = PROGRESS.open("a", encoding="utf-8")
    ok = err = 0
    for i, r in enumerate(todo, 1):
        uid, name = r["user_id"], r["name"]
        res = sessiond_client.cmd(args.account, {"cmd": "unfriend", "userId": uid, "dryRun": (not live)}, timeout=60)
        good = bool(res.get("ok")) and not res.get("error")
        ok += good; err += (not good)
        pf.write(json.dumps({"ts": int(time.time()), "user_id": uid, "name": name,
                             "ok": good, "dryRun": (not live), "resp": res}, ensure_ascii=False) + "\n")
        pf.flush()
        print(f"[{i}/{len(todo)}] {'OK ' if good else 'ERR'} {uid} {name[:40]} :: {json.dumps(res, ensure_ascii=False)[:110]}")
        if live and i < len(todo):
            time.sleep(random.uniform(args.min_gap, args.max_gap))
    pf.close()
    print(f"\nDONE ok={ok} err={err} mode={'LIVE' if live else 'DRY-RUN'}")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
