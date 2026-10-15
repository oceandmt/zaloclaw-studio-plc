"""DB layer for Pipeline G2G-S01 (Signal Commodities Daily).

Fully additive companion to db.py: it re-uses db._conn / db._LOCK / the same
SQLite file (data/zs.db) but owns its OWN tables (s01_*). Nothing in db.py or
the G2G autobot schema is touched.

Pipeline G2G-S01 mirrors Pipeline G2G (forward a message from a source group A
to a destination group B) but is specialised for the "<NHOM_NGUON_B> Hằng
Ngày" group:

  * a REPLY in A   -> find the forwarded copy in B and reply to THAT message id
                      (native quote), falling back to a context line.
  * an @All in A   -> forward "@All <body>" to B with a real @All mention.
  * an @Name in A  -> skipped by default (fw_name=0).
"""
from __future__ import annotations

import time

import db

SCHEMA_S01 = """
CREATE TABLE IF NOT EXISTS s01_rules (
  id TEXT PRIMARY KEY,
  name TEXT DEFAULT '',
  account_id TEXT,
  src_group_id TEXT,
  src_group_name TEXT DEFAULT '',
  dst_group_id TEXT,
  dst_group_name TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1,
  dry_run INTEGER DEFAULT 1,
  include_self INTEGER DEFAULT 0,
  prefix TEXT DEFAULT '',
  max_per_hour INTEGER DEFAULT 60,
  gap_seconds INTEGER DEFAULT 5,
  group_window_ms INTEGER DEFAULT 2000,
  reply_mode TEXT DEFAULT 'quote',
  reply_quote_ms INTEGER DEFAULT 0,
  fw_replies INTEGER DEFAULT 1,
  fw_all INTEGER DEFAULT 1,
  fw_name INTEGER DEFAULT 0,
  last_seen_msg_id TEXT DEFAULT '',
  last_run_at INTEGER,
  forwarded_count INTEGER DEFAULT 0,
  skipped_count INTEGER DEFAULT 0,
  error TEXT DEFAULT '',
  created_at INTEGER,
  updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS s01_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_id TEXT,
  src_group_id TEXT,
  src_msg_id TEXT DEFAULT '',
  src_name TEXT DEFAULT '',
  text TEXT DEFAULT '',
  status TEXT DEFAULT '',
  dst_msg_id TEXT DEFAULT '',
  error TEXT DEFAULT '',
  ts INTEGER
);
-- Dedup ledger mirror of forward_seen, kept separate so S01 replay/restart can
-- never collide with (or be silenced by) the G2G ledger for the same group.
CREATE TABLE IF NOT EXISTS s01_seen (
  account_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  msg_key TEXT NOT NULL,
  ts INTEGER NOT NULL,
  PRIMARY KEY (account_id, group_id, msg_key)
);
CREATE INDEX IF NOT EXISTS ix_s01_seen_ts ON s01_seen(ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_s01_log
  ON s01_log(rule_id, src_group_id, src_msg_id) WHERE src_msg_id <> '';
"""

_S01_FIELDS = {
    "name", "account_id", "src_group_id", "src_group_name", "dst_group_id",
    "dst_group_name", "enabled", "dry_run", "include_self", "prefix",
    "max_per_hour", "gap_seconds", "group_window_ms", "reply_mode",
    "reply_quote_ms", "fw_replies", "fw_all", "fw_name", "last_seen_msg_id",
    "last_run_at", "forwarded_count", "skipped_count", "error",
}


def init() -> None:
    """Create the S01 tables (idempotent)."""
    with db._LOCK, db._conn() as c:
        c.executescript(SCHEMA_S01)


def create_rule(rid: str, **kw) -> None:
    now = int(time.time())
    defaults = {"id": rid, "name": "", "account_id": None, "src_group_id": None,
                "src_group_name": "", "dst_group_id": None, "dst_group_name": "",
                "enabled": 1, "dry_run": 1, "include_self": 0, "prefix": "",
                "max_per_hour": 60, "gap_seconds": 5, "group_window_ms": 2000,
                "reply_mode": "quote", "reply_quote_ms": 0,
                "fw_replies": 1, "fw_all": 1, "fw_name": 0,
                "last_seen_msg_id": "",
                "last_run_at": None, "forwarded_count": 0, "skipped_count": 0,
                "error": "", "created_at": now, "updated_at": now}
    row = {**defaults, **{k: v for k, v in kw.items() if k in _S01_FIELDS or k == "id"}}
    cols = ",".join(row.keys())
    ph = ",".join(":" + k for k in row.keys())
    with db._LOCK, db._conn() as c:
        c.execute(f"INSERT INTO s01_rules({cols}) VALUES({ph})", row)


def get_rule(rid: str) -> dict | None:
    with db._LOCK, db._conn() as c:
        r = c.execute("SELECT * FROM s01_rules WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None


def list_rules() -> list[dict]:
    with db._LOCK, db._conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM s01_rules ORDER BY created_at DESC")]


def update_rule(rid: str, **kw) -> None:
    fields = {k: v for k, v in kw.items() if k in _S01_FIELDS}
    if not fields:
        return
    fields["updated_at"] = int(time.time())
    sets = ", ".join(f"{k}=?" for k in fields)
    with db._LOCK, db._conn() as c:
        c.execute(f"UPDATE s01_rules SET {sets} WHERE id=?", (*fields.values(), rid))


def bump_rule(rid: str, *, forwarded: int = 0, skipped: int = 0, last_seen_msg_id: str = "",
              error: str | None = None) -> None:
    sets = ["last_run_at=?", "forwarded_count=forwarded_count+?", "skipped_count=skipped_count+?",
            "updated_at=?"]
    now = int(time.time())
    vals: list = [now, forwarded, skipped, now]
    if last_seen_msg_id:
        sets.append("last_seen_msg_id=?")
        vals.append(last_seen_msg_id)
    if error is not None:
        sets.append("error=?")
        vals.append(error[:300])
    with db._LOCK, db._conn() as c:
        c.execute(f"UPDATE s01_rules SET {', '.join(sets)} WHERE id=?", (*vals, rid))


def delete_rule(rid: str) -> None:
    with db._LOCK, db._conn() as c:
        c.execute("DELETE FROM s01_rules WHERE id=?", (rid,))


def add_log(rule_id: str, src_group_id: str, src_name: str, text: str, status: str,
            dst_msg_id: str = "", src_msg_id: str = "", error: str = "") -> bool:
    """Insert a log row. Returns False when the DB unique guard rejects a dup
    (same rule+group+src_msg_id), mirroring db.add_forward_log."""
    import sqlite3
    now = int(time.time())
    try:
        with db._LOCK, db._conn() as c:
            c.execute("INSERT INTO s01_log(rule_id, src_group_id, src_msg_id, src_name,"
                      " text, status, dst_msg_id, error, ts) VALUES(?,?,?,?,?,?,?,?,?)",
                      (rule_id, str(src_group_id or ""), str(src_msg_id or ""),
                       (src_name or "")[:200], (text or "")[:2000], status,
                       str(dst_msg_id or ""), (error or "")[:300], now))
        return True
    except sqlite3.Error:
        return False


def list_log(limit: int = 200, rule_id: str | None = None) -> list[dict]:
    with db._LOCK, db._conn() as c:
        if rule_id:
            rows = c.execute("SELECT * FROM s01_log WHERE rule_id=? ORDER BY id DESC LIMIT ?",
                             (rule_id, limit))
        else:
            rows = c.execute("SELECT * FROM s01_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def count_sent_since(rule_id: str, since_ts: int) -> int:
    with db._LOCK, db._conn() as c:
        r = c.execute("SELECT COUNT(*) FROM s01_log WHERE rule_id=? AND status='sent' AND ts>=?",
                      (rule_id, since_ts)).fetchone()
    return int(r[0]) if r else 0


def find_by_src(rule_id: str, src_group_id: str, src_msg_id: str) -> dict | None:
    """Look up the forwarded record for a source message (map a reply's parent
    in A to the copy we sent into B)."""
    with db._LOCK, db._conn() as c:
        r = c.execute("SELECT * FROM s01_log WHERE rule_id=? AND src_group_id=? AND src_msg_id=?"
                      " AND dst_msg_id<>'' ORDER BY id DESC LIMIT 1",
                      (rule_id, str(src_group_id or ""), str(src_msg_id or ""))).fetchone()
    return dict(r) if r else None


def find_by_src_content(rule_id: str, src_group_id: str, content: str) -> dict | None:
    """Content-based fallback: find our forwarded copy whose logged text matches
    the replied-to content (used when the quote carries no usable msgId)."""
    text = (content or "").strip()
    if not text:
        return None
    with db._LOCK, db._conn() as c:
        r = c.execute("SELECT * FROM s01_log WHERE rule_id=? AND src_group_id=?"
                      " AND dst_msg_id<>'' AND text LIKE ? ORDER BY id DESC LIMIT 1",
                      (rule_id, str(src_group_id or ""), "%" + text[:120] + "%")).fetchone()
    return dict(r) if r else None


def last_src_msg_id(rule_id: str, src_group_id: str) -> str:
    with db._LOCK, db._conn() as c:
        r = c.execute("SELECT src_msg_id FROM s01_log WHERE rule_id=? AND src_group_id=?"
                      " AND src_msg_id<>'' ORDER BY id DESC LIMIT 1",
                      (rule_id, str(src_group_id or ""))).fetchone()
    return str(r[0]) if r else ""


def seen(account_id: str, group_id: str, msg_key: str, ttl_seconds: int = 30 * 86400) -> bool:
    """Durable dedup ledger. True = first time (handle it); False = already seen."""
    if not msg_key:
        return True
    now = int(time.time())
    with db._LOCK, db._conn() as c:
        r = c.execute("SELECT ts FROM s01_seen WHERE account_id=? AND group_id=? AND msg_key=?",
                      (account_id, str(group_id or ""), msg_key)).fetchone()
        if r and (now - int(r[0])) <= ttl_seconds:
            return False
        c.execute("INSERT INTO s01_seen(account_id, group_id, msg_key, ts) VALUES(?,?,?,?)"
                  " ON CONFLICT(account_id, group_id, msg_key) DO UPDATE SET ts=excluded.ts",
                  (account_id, str(group_id or ""), msg_key, now))
        return True


def seen_recent_count(account_id: str, group_id: str) -> int:
    with db._LOCK, db._conn() as c:
        r = c.execute("SELECT COUNT(*) FROM s01_seen WHERE account_id=? AND group_id=?",
                      (account_id, str(group_id or ""))).fetchone()
    return int(r[0]) if r else 0
