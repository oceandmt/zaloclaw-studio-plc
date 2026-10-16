#!/usr/bin/env python3
"""Regression: Pipeline G2G-S01 (additive mirror of Pipeline G2G).

Pipeline G2G-S01 is a SEPARATE pipeline that reuses the G2G framework but is
specialised for the "<NHOM_NGUON_B>" source group. It must:
  1) own its OWN tables (s01_*) and never touch the G2G autobot schema;
  2) resolve a reply in A to the forwarded copy in B (msgId -> msgId), with a
     content-search fallback;
  3) strip the original @All token and re-emit a real @All mention when
     forwarding an @All message;
  4) skip @Name messages by default;
  5) contribute its source groups to the SHARED watch allowlist (core);
  6) expose the app routes + Jinja tab so the UI is wired.

SAFETY: uses an ISOLATED temp database. It never reads or writes the operator's
live data/zs.db.

Run:  webapp/.venv/bin/python scripts/test_pgs01.py
"""
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "webapp"))
import db  # noqa: E402

# --- ISOLATION: point db at a throw-away sqlite file BEFORE any db call ------
_LIVE_DB = db.DB_PATH
_TMP = Path(tempfile.mkdtemp(prefix="zs-pgs01-test-"))
db.DATA = _TMP
db.DB_PATH = _TMP / "test.db"
assert db.DB_PATH != _LIVE_DB, "refusing to run against the live DB"
db.init()

import core  # noqa: E402
import pgs01  # noqa: E402
import pgs01_db  # noqa: E402

pgs01_db.init()

fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + "  " + name + ((" :: " + extra) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


# 1) additive DB layer: own tables, CRUD round-trip --------------------------
rid = "s01_test0001"
pgs01_db.create_rule(rid, name="T", account_id="nick1",
                     src_group_id="A111", src_group_name="Src",
                     dst_group_id="B222", dst_group_name="Dst",
                     enabled=1, dry_run=1, max_per_hour=60, gap_seconds=0)
r = pgs01_db.get_rule(rid)
check("db create/get", r and r["src_group_id"] == "A111" and r["dst_group_id"] == "B222", str(r))
check("db default fw_name off", r and int(r["fw_name"]) == 0, str(r))
pgs01_db.update_rule(rid, prefix="[S01] ", fw_name=1)
check("db update", pgs01_db.get_rule(rid)["prefix"] == "[S01] ")
check("db list has rule", any(x["id"] == rid for x in pgs01_db.list_rules()))
with db._conn() as c:
    tbls = {t[0] for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
check("db owns s01_* tables", {"s01_rules", "s01_log", "s01_seen"} <= tbls, str(sorted(tbls)))

# 2) reply resolution: msgId -> forwarded copy in B --------------------------
pgs01_db.add_log(rid, "A111", "An", "giá thép tăng", "sent",
                 dst_msg_id="DST999", src_msg_id="SRC888")
row = pgs01_db.find_by_src(rid, "A111", "SRC888")
check("reply by msgId", (row or {}).get("dst_msg_id") == "DST999", str(row))
row2 = pgs01_db.find_by_src_content(rid, "A111", "giá thép tăng")
check("reply by content fallback", (row2 or {}).get("dst_msg_id") == "DST999", str(row2))

# _reply_by_search builds a native quote object pointing at the dst msgId
rule = pgs01_db.get_rule(rid)
ev = {"quote": {"globalMsgId": "SRC888", "msg": "giá thép tăng", "ownerId": "u1",
                "ts": 1700000000000}, "text": "ok"}
rprefix, qobj = pgs01._reply_by_search(rule, ev, "A111")
check("_reply_by_search native quote", qobj and qobj.get("msgId") == "DST999", str(qobj))
check("_reply_by_search no dup token", rprefix == "", repr(rprefix))

# 3) @All detection + token stripping + real mention on resend ---------------
check("mentions_all by uid=-1", pgs01._mentions_all([{"uid": "-1", "pos": 0, "len": 4, "type": 1}]))
check("mentions_all by type=1", pgs01._mentions_all([{"uid": "x", "type": 1}]))
check("mentions_all false for name", not pgs01._mentions_all([{"uid": "5", "type": 0}]))
alltext = "@All giá dầu Brent tăng"
stripped = pgs01._strip_mentions(alltext, [{"pos": 0, "len": 4, "uid": "-1", "type": 1}])
check("strip @All token", stripped == "giá dầu Brent tăng", repr(stripped))
blocks = pgs01._all_blocks(stripped, [])
check("all block re-adds mention",
      blocks[0]["content"].startswith("@All ") and
      blocks[0]["mentions"][0]["uid"] == "-1" and blocks[0]["mentions"][0]["type"] == 1,
      str(blocks))
check("@Name detected", pgs01._has_name_mention([{"uid": "77", "pos": 0, "len": 5, "type": 0}]))
check("@All not a name mention", not pgs01._has_name_mention([{"uid": "-1", "type": 1}]))

# 4) shared watch allowlist includes S01 source groups -----------------------
core._bots = {}
groups = core._bot_watch_groups("nick1")
check("watch union includes S01 src", "A111" in groups, str(groups))

# 5) role gate: S01 rides with the G2G process by default --------------------
check("role gate exists", hasattr(core, "role_allows_s01"))
check("S01 watch groups helper", pgs01._s01_watch_groups("nick1") == ["A111"],
      str(pgs01._s01_watch_groups("nick1")))

# 6) app.py routes + Jinja tab are wired -------------------------------------
app_src = (HERE / "webapp" / "app.py").read_text()
check("app has /ui/pgs01", '"/ui/pgs01"' in app_src)
check("app has /pgs01/new", '"/pgs01/new"' in app_src)
check("app has /pgs01/bot/watch", '"/pgs01/bot/watch"' in app_src)
check("app ui_tab allowlist has pgs01", re.search(r'allowed\s*=\s*\{[^}]*"pgs01"', app_src) is not None)
check("app imports pgs01", "import pgs01" in app_src)
base_src = (HERE / "webapp" / "templates" / "base.html").read_text()
check("base.html nav has S01 tab", 'data-tab="pgs01"' in base_src)
check("base.html S01 js", "function pgsRefresh" in base_src)

# 7) relay.mjs carries mentions through msgToEvent -> sendBlocks -------------
relay = (HERE / "bridge" / "relay.mjs").read_text()
check("relay msgToEvent mentions", "mentions," in relay)
check("relay sendBlocks passes mentions", "mentions: men" in relay)

# 8) album coalescing: a burst of captionless images -> ONE multi-url part ---
captured = []
pgs01._enqueue = lambda item: captured.append(item)
arule = {"id": rid, "account_id": "nick1", "dst_group_id": "B222",
         "fw_replies": 1, "group_window_ms": 1500}
_check = "album"
part_a = {"kind": "image", "url": "http://x/a.jpg", "caption": ""}
part_b = {"kind": "image", "url": "http://x/b.jpg", "caption": ""}
part_c = {"kind": "image", "url": "http://x/c.jpg", "caption": ""}
ev_img = {"uidFrom": "U1", "name": "An", "mentions": []}
ev_img2 = {"uidFrom": "U1", "name": "An", "mentions": []}
ev_img3 = {"uidFrom": "U1", "name": "An", "mentions": []}

check("album candidate bare image", pgs01._album_candidate(ev_img, part_a))
check("album rejects captioned image",
      not pgs01._album_candidate(ev_img, {"kind": "image", "url": "http://x/a.jpg", "caption": "hi"}))
check("album rejects reply image",
      not pgs01._album_candidate({"uidFrom": "U1", "quote": {"msgId": "q1"}, "mentions": []}, part_a))
check("album rejects text", not pgs01._album_candidate(ev_img, {"kind": "text", "text": "hi"}))

pgs01._dispatch(arule, ev_img, part_a, "", "A111", "M1", [], "image", [])
check("album buffers (no immediate enqueue)", captured == [], str(captured))
pgs01._dispatch(arule, ev_img2, part_b, "", "A111", "M2", [], "image", [])
pgs01._dispatch(arule, ev_img3, part_c, "", "A111", "M3", [], "image", [])
key = pgs01._album_key(arule, "A111", ev_img)
check("album accumulated 3 urls", sorted(pgs01._ALBUM[key]["urls"]) ==
      ["http://x/a.jpg", "http://x/b.jpg", "http://x/c.jpg"], str(pgs01._ALBUM.get(key)))
check("album tracked all src ids",
      sorted(pgs01._ALBUM[key]["item"]["src_msg_ids"]) == ["M1", "M2", "M3"])

pgs01._album_flush(key)
check("album flush emits ONE item", len(captured) == 1, str(len(captured)))
if captured:
    it = captured[0]
    check("album item is single image part", len(it["parts"]) == 1 and it["parts"][0]["kind"] == "image",
          str(it.get("parts")))
    check("album item carries 3 urls", len(it["parts"][0].get("urls") or []) == 3, str(it["parts"]))
    check("album item keeps all src ids", sorted(it["src_msg_ids"]) == ["M1", "M2", "M3"], str(it.get("src_msg_ids")))
    check("album log label", it.get("log_text") == "[album 3 ảnh]", str(it.get("log_text")))

# different sender must NOT join the same album
captured.clear()
ev_other = {"uidFrom": "U2", "name": "Binh", "mentions": []}
pgs01._dispatch(arule, ev_img, part_a, "", "A111", "M1", [], "image", [])
pgs01._dispatch(arule, ev_other, part_b, "", "A111", "M4", [], "image", [])
check("album keeps senders separate", len(pgs01._ALBUM) == 2, str(list(pgs01._ALBUM.keys())))
for k in list(pgs01._ALBUM.keys()):
    pgs01._album_flush(k)
check("album sent as two messages for two senders", len(captured) == 2, str(len(captured)))

# disabled window -> legacy per-image forward (no buffering)
captured.clear()
arule_off = dict(arule, group_window_ms=0)
pgs01._dispatch(arule_off, ev_img, part_a, "", "A111", "M9", [], "image", [])
check("album disabled forwards immediately", len(captured) == 1 and pgs01._ALBUM == {},
      f"captured={len(captured)} album={pgs01._ALBUM}")

# text message bypasses album entirely
captured.clear()
pgs01._dispatch(arule, {"uidFrom": "U1", "name": "An", "mentions": []}, None, "giá thép", "A111", "MT", [], "", [])
check("text not buffered into album", len(captured) == 1 and pgs01._ALBUM == {},
      f"captured={len(captured)} album={pgs01._ALBUM}")
pgs01._ALBUM.clear()

# ordering guard: a trailing captioned image flushes the buffered burst FIRST
captured.clear()
pgs01._dispatch(arule, ev_img, part_a, "", "A111", "T1", [], "image", [])
pgs01._dispatch(arule, ev_img2, part_b, "", "A111", "T2", [], "image", [])
cap_part = {"kind": "image", "url": "http://x/z.jpg", "caption": "ghi chú"}
pgs01._dispatch(arule, ev_img3, cap_part, "ghi chú", "A111", "T3", [], "image", [])
check("ordering: burst flushed before captioned image", len(captured) == 2, str(len(captured)))
if len(captured) == 2:
    check("ordering: buffered album first", captured[0].get("parts", [{}])[0].get("urls") ==
          ["http://x/a.jpg", "http://x/b.jpg"], str(captured[0].get("parts")))
    check("ordering: captioned image second", captured[1].get("text") == "ghi chú",
          str(captured[1].get("text")))
check("ordering: buffer emptied", pgs01._ALBUM == {}, str(pgs01._ALBUM))

# cleanup temp db ------------------------------------------------------------
try:
    for f in _TMP.glob("test.db*"):
        f.unlink(missing_ok=True)
    _TMP.rmdir()
except Exception:
    pass

print()
print("FAILS:", "none" if not fails else fails)
sys.exit(1 if fails else 0)
