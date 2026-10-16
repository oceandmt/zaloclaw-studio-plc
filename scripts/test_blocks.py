#!/usr/bin/env python3
"""Regression tests for the ordered BLOCK composer (P0).

Run:  webapp/.venv/bin/python scripts/test_blocks.py
Covers: parse_blocks legacy fallback, block normalization, per-recipient
compile (placeholders), image batching, and bridge argv building. No Zalo traffic.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "webapp"))
import core  # noqa: E402
import db  # noqa: E402

# --- ISOLATION: point db at a throw-away sqlite file BEFORE any db call ------
# This suite resolves campaign settings via core.* (which read the `settings`
# table), so it must init its OWN schema — never lean on the operator's live
# data/zs.db (a fresh clone has none). Probe image files also stay out of the
# live data/media dir.
_LIVE_DB = db.DB_PATH
_TMP = Path(tempfile.mkdtemp(prefix="zs-blocks-test-"))
db.DATA = _TMP
db.DB_PATH = _TMP / "test.db"
assert db.DB_PATH != _LIVE_DB, "refusing to run against the live DB"
db.init()
_PROBE = _TMP

fails = 0


def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


# --- parse: new blocks stored -> used verbatim -------------------------------
newc = {"blocks": core.blocks_to_json([
    {"type": "text", "content": "Chào {name}"},
    {"type": "image", "src": "/tmp/none.jpg", "caption": "ảnh {salutation}"},
    {"type": "text", "content": "Thanks"},
    {"type": "link", "url": "https://x.vn", "caption": "xem {name}"},
    {"type": "bogus", "content": "drop me"},
])}
pb = core.parse_blocks(newc)
check("new blocks parsed (junk dropped)", [b["type"] for b in pb] == ["text", "image", "text", "link"])
check("summary", core.blocks_summary(pb) == "2 text · 1 ảnh · 1 link")

# --- parse: legacy campaign (blocks column empty) -> rebuilt -----------------
leg = {"body_template": "Xin chào {salutation} {name}",
       "images": json.dumps([str(HERE / "data" / "media" / "_nope_not_here.jpg")]),  # missing -> dropped
       "blocks": "[]", "url": "https://z.vn"}
pl = core.parse_blocks(leg)
check("legacy rebuilt: text + link (missing image dropped)",
      [b["type"] for b in pl] == ["text", "link"])
check("legacy keeps URL", pl[-1]["url"] == "https://z.vn")

# --- parse: truly empty ------------------------------------------------------
check("empty campaign -> []", core.parse_blocks({"blocks": "[]", "body_template": ""}) == [])

# --- compile: placeholders per gender ---------------------------------------
img = _PROBE / "_pytest_img.png"
img.write_bytes(b"\x89PNG\r\n\x1a\n")  # just needs to exist
blocks = [
    {"type": "text", "content": "Chào {salutation} {name}"},
    {"type": "image", "src": str(img), "caption": "ảnh {name}"},
    {"type": "link", "url": "https://y.vn", "caption": ""},
]
male = core.compile_blocks(blocks, name="Minh", gender="male")
female = core.compile_blocks(blocks, name="Lan", gender="female")
check("male salutation", male[0]["content"] == "Chào Anh Minh")
check("female salutation", female[0]["content"] == "Chào Chị Lan")
check("image caption personalized", male[1]["caption"] == "ảnh Minh")
check("image kept (exists)", male[1]["src"] == str(img))
check("missing image dropped", core.compile_blocks([{"type": "image", "src": "/nope.jpg"}]) == [])
img.unlink(missing_ok=True)

# --- image batching ----------------------------------------------------------
b2 = core.parse_block_list(core.blocks_to_json([
    {"type": "image", "src": "/tmp/a.jpg"},
    {"type": "image", "src": "/tmp/b.jpg", "caption": "cap2"},
    {"type": "text", "content": "sau"},
]))


class _FakePath(type(Path())):  # noqa
    pass


# patch Path.exists for batching test
_orig_exists = Path.exists
Path.exists = lambda self: True  # type: ignore
try:
    batched = core._img_batches(b2)
finally:
    Path.exists = _orig_exists  # type: ignore
check("consecutive images -> one album", len(batched) == 2 and batched[0]["type"] == "image"
      and len(batched[0]["images"]) == 2)
check("batch caption joins", batched[0]["caption"] == "cap2")

# --- bridge argv -------------------------------------------------------------
job_blocks = {"kind": "message", "thread": "1", "type": "user", "blocks": blocks}
a1 = core.send_bridge_args(job_blocks, "nick1")
check("blocks job uses --blocks", "--blocks" in a1 and "--images" not in a1 and "--text" not in a1)
# legacy flags need a REAL file: send_bridge_args drops image paths that don't
# exist, so a hard-coded /tmp path made this assertion environment-fragile.
legacy_img = _PROBE / "_pytest_legacy.png"
legacy_img.write_bytes(b"\x89PNG\r\n\x1a\n")
try:
    job_legacy = {"kind": "message", "thread": "1", "type": "user", "url": "https://y.vn",
                  "images": [str(legacy_img)], "text": "hi", "file_caption": "cap"}
    a2 = core.send_bridge_args(job_legacy, "nick1")
    check("legacy job uses flags", "--images" in a2 and "--text" in a2 and "--blocks" not in a2)
finally:
    legacy_img.unlink(missing_ok=True)

shutil.rmtree(_PROBE, ignore_errors=True)

print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
