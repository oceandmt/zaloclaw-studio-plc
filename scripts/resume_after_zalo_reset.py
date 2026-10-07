#!/usr/bin/env python3
"""One-shot recovery after the Zalo find-users cooldown clears.

Root cause of the 2026-10-04 incident: campaign c_2fefdf92 ("03/10 - <NHOM_NGUON_A>
bot") sent individual (thread_type=user) messages built from RAW phones whose
Zalo userId was never resolved, so Zalo returned "Tham số không hợp lệ" (invalid
parameter) for every target -> the transient-retry path requeued forever and
hot-looped ~55 jobs/second.

This script:
  1. resolves the campaign's unresolved pending phones -> zalo_user_id,
  2. resumes the campaign (start) so it sends only once ids are present.

Safe to re-run: it only touches PENDING targets and is a no-op when nothing is
left to resolve.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webapp"))
import db  # noqa: E402
import core  # noqa: E402

CID = "c_2fefdf92"
ACCT = "nick1"


def main() -> int:
    camp = db.get_campaign(CID)
    if not camp:
        print(f"[{time.strftime('%H:%M:%S')}] campaign {CID} not found; nothing to do")
        return 0

    rows = db.pending_targets(CID)
    phones = [r["phone"] for r in rows
              if r["phone"] and not r.get("zalo_user_id") and core.is_real_phone(r["phone"])]
    print(f"[{time.strftime('%H:%M:%S')}] unresolved pending phones: {len(phones)}")

    resolved = 0
    if phones:
        # find-users returns "" users when the account is still cooldown-blocked,
        # so try; if nothing resolves, leave the campaign paused for a manual retry.
        resolved = core.resolve_phones(ACCT, phones, cid=CID)
    with_zid = sum(1 for r in db.pending_targets(CID) if r.get("zalo_user_id"))
    pending = len(db.pending_targets(CID))
    print(f"[{time.strftime('%H:%M:%S')}] resolved={resolved}; pending_with_zid={with_zid}/{pending}")

    if with_zid == 0 and pending > 0:
        core.log(f"RECOVERY skipped :: {CID} still unresolved (Zalo cooldown?)")
        print("No ids resolved (Zalo still throttling?) -> leaving campaign PAUSED.")
        return 1

    core.log(f"RECOVERY resolving {CID}: {with_zid}/{pending} have zalo_user_id; starting send")
    res = core.start_campaign(CID)
    print(f"[{time.strftime('%H:%M:%S')}] start_campaign -> {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
