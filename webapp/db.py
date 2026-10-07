"""SQLite storage for zaloclaw-studio (MVP, single-tenant)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "zs.db"
_LOCK = threading.RLock()
_KEEPER: sqlite3.Connection | None = None
_KEEPER_LOCK = threading.Lock()


def _ensure_keeper() -> None:
    """Hold ONE idle connection open for the process lifetime.

    In WAL mode SQLite deletes the ``-wal`` file when the LAST connection to a
    database closes. Every short-lived connection (this module opens/closes one
    per query) then pays a ~0.5 s fsync recreating it — which dominates write
    latency on this host. An idle keeper keeps the WAL hot, so per-query
    connections commit in ~1 ms instead of ~0.6 s (≈600× on the write path).

    The keeper is never used for queries — it only anchors the WAL. Failure to
    create it is non-fatal (best-effort speedup only).
    """
    global _KEEPER
    k = _KEEPER
    if k is not None:
        try:
            k.execute("SELECT 1")
            return
        except sqlite3.Error:
            try:
                k.close()
            except Exception:
                pass
    with _KEEPER_LOCK:
        if _KEEPER is None:
            try:
                DATA.mkdir(parents=True, exist_ok=True)
                k = sqlite3.connect(DB_PATH, timeout=30)
                k.execute("PRAGMA journal_mode=WAL")
                k.execute("PRAGMA synchronous=NORMAL")
                _KEEPER = k
            except sqlite3.Error:
                pass


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Yield a fresh SQLite connection and ALWAYS close it on exit.

    NOTE: `with sqlite3.connect(...) as c:` only commits/rolls back the
    transaction — it does NOT close the connection. We must close explicitly,
    otherwise every query leaks 1 connection + 2 FDs and the process hits the
    open-file limit (``unable to open database file``).
    """
    _ensure_keeper()
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL. The default (FULL) makes every COMMIT fsync synchronously
        # (~0.6 s/write on this host). NORMAL lets WAL fsync only at checkpoints;
        # worst case on power loss is losing the last transaction(s) — no
        # corruption. Turns a 0.6 s write into ~1 ms.
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        yield c
        c.commit()
    except BaseException:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    finally:
        c.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  name TEXT,
  user_id TEXT,
  display_name TEXT,
  creds_path TEXT,
  proxy TEXT,
  enabled INTEGER DEFAULT 1,
  status TEXT DEFAULT 'logged_out',
  last_seen INTEGER,
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT,
  zalo_user_id TEXT,
  display_name TEXT,
  gender TEXT DEFAULT '',
  tags TEXT DEFAULT '',
  resolved INTEGER DEFAULT 0,
  last_error TEXT,
  created_at INTEGER,
  UNIQUE(phone)
);
CREATE TABLE IF NOT EXISTS campaigns (
  id TEXT PRIMARY KEY,
  name TEXT,
  kind TEXT,                    -- message | friend
  account_id TEXT,
  thread_id TEXT,               -- group id for group campaigns
  thread_type TEXT DEFAULT 'user',
  body_template TEXT,
  url TEXT,
  media_path TEXT,
  images TEXT DEFAULT '[]',      -- JSON list of uploaded image paths (legacy)
  blocks TEXT DEFAULT '[]',      -- JSON ordered block list [{type,content|src|caption|url}]
  status TEXT DEFAULT 'draft',  -- draft | running | paused | done | error
  dry_run INTEGER DEFAULT 1,
  created_at INTEGER,
  started_at INTEGER,
  finished_at INTEGER,
  stats TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id TEXT,
  contact_id INTEGER,
  phone TEXT,
  zalo_user_id TEXT,
  status TEXT DEFAULT 'pending', -- pending | sent | failed | skipped
  error TEXT,
  msg_id TEXT,
  updated_at INTEGER,
  UNIQUE(campaign_id, phone)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER,
  account_id TEXT,
  campaign_id TEXT,
  kind TEXT,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS groups (
  account_id TEXT,
  group_id TEXT,
  name TEXT,
  total_member INTEGER DEFAULT 0,
  member_count INTEGER DEFAULT 0,
  admin_count INTEGER DEFAULT 0,
  avt TEXT,
  updated_at INTEGER,
  PRIMARY KEY(account_id, group_id)
);
CREATE TABLE IF NOT EXISTS members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT,
  group_id TEXT,
  user_id TEXT,
  display_name TEXT,
  global_id TEXT,
  is_admin INTEGER DEFAULT 0,
  scanned_at INTEGER,
  UNIQUE(group_id, user_id)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS contact_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  source TEXT DEFAULT '',
  note TEXT DEFAULT '',
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS file_members (
  file_id INTEGER,
  phone TEXT,
  PRIMARY KEY(file_id, phone)
);
CREATE TABLE IF NOT EXISTS group_scan_progress (
  group_id TEXT PRIMARY KEY,
  account_id TEXT,
  name TEXT,
  status TEXT DEFAULT 'pending',
  total INTEGER DEFAULT 0,
  fetched INTEGER DEFAULT 0,
  partial INTEGER DEFAULT 0,
  error TEXT DEFAULT '',
  started_at INTEGER,
  updated_at INTEGER,
  finished_at INTEGER
);
-- Autobot: forward messages from a source group (A) to a destination group (B).
-- Listen-only on A; the BOT itself is what sends into B (no auto-reply).
CREATE TABLE IF NOT EXISTS forward_rules (
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
  reply_mode TEXT DEFAULT 'text',
  reply_quote_ms INTEGER DEFAULT 0,
  last_seen_msg_id TEXT DEFAULT '',
  last_run_at INTEGER,
  forwarded_count INTEGER DEFAULT 0,
  skipped_count INTEGER DEFAULT 0,
  error TEXT DEFAULT '',
  created_at INTEGER,
  updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS forward_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_id TEXT,
  src_group_id TEXT,
  src_msg_id TEXT DEFAULT '',
  src_name TEXT DEFAULT '',
  text TEXT DEFAULT '',
  status TEXT DEFAULT '',
  dst_msg_id TEXT DEFAULT '',
  ts INTEGER
);
-- Dedup ledger for the G2G forwarder. Persists across restarts so a message
-- replayed on listener reconnect is NOT forwarded twice. msg_key is
-- 'm:<msgId|realMsgId>' or 'h:<sha1>' fallback when the event carries no id.
CREATE TABLE IF NOT EXISTS forward_seen (
  account_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  msg_key TEXT NOT NULL,
  ts INTEGER NOT NULL,
  PRIMARY KEY (account_id, group_id, msg_key)
);
CREATE INDEX IF NOT EXISTS ix_fwd_seen_ts ON forward_seen(ts);
"""


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(SCHEMA)
        # migrations (idempotent): add columns that older DBs may lack
        for stmt in (
            "ALTER TABLE contacts ADD COLUMN gender TEXT DEFAULT ''",
            "ALTER TABLE campaigns ADD COLUMN tags TEXT DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN images TEXT DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN blocks TEXT DEFAULT '[]'",
            "ALTER TABLE forward_log ADD COLUMN src_msg_id TEXT DEFAULT ''",
            # forward-failure reason, so a status='error' row is diagnosable later
            "ALTER TABLE forward_log ADD COLUMN error TEXT DEFAULT ''",
            # grouping window: coalesce an image + its follow-up text (sent as
            # separate messages) into one post. 0 = disabled (old behaviour).
            "ALTER TABLE forward_rules ADD COLUMN group_window_ms INTEGER DEFAULT 2000",
            # reply handling: how to relay a message that REPLIES to another.
            #   'text'  -> prefix a "↩ name · hh:mm: <quote>" context line (safe)
            #   'quote' -> native Zalo quote when the parent is mapped, else text
            #   'skip'  -> drop replies entirely
            # reply_quote_ms = 1 -> include the parent's original timestamp in the context line.
            "ALTER TABLE forward_rules ADD COLUMN reply_mode TEXT DEFAULT 'text'",
            "ALTER TABLE forward_rules ADD COLUMN reply_quote_ms INTEGER DEFAULT 0",
            # canonical identity for cross-campaign de-dup (F3/F4)
            "ALTER TABLE targets ADD COLUMN ident TEXT DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS ix_targets_ident ON targets(ident)",
            # DB-level duplicate guard for forwarded messages (partial: only rows
            # that carry a source msg id). Catches double-writes the ledger misses.
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_fwd_log"
            " ON forward_log(rule_id, src_group_id, src_msg_id) WHERE src_msg_id <> ''",
        ):
            try:
                c.execute(stmt)
            except sqlite3.Error:
                pass
        # Backfill canonical `ident` for rows written before the column existed
        # (F3): lets cross-campaign de-dup work on existing data too.
        try:
            for row in c.execute("SELECT id, phone, zalo_user_id FROM targets"
                                 " WHERE ident IS NULL OR ident=''").fetchall():
                c.execute("UPDATE targets SET ident=? WHERE id=?",
                          (canon_ident(row[1], row[2]), row[0]))
        except sqlite3.Error:
            pass


# ------------------------------------------------------------------ accounts

def upsert_account(aid: str, **kw) -> None:
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO accounts(id, created_at) VALUES(?,?) ON CONFLICT(id) DO NOTHING",
                  (aid, int(time.time())))
        fields = {k: v for k, v in kw.items() if v is not None}
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE accounts SET {sets} WHERE id=?", (*fields.values(), aid))


def list_accounts() -> list[dict]:
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM accounts ORDER BY id")]


def get_account(aid: str) -> dict | None:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None


def delete_account(aid: str) -> None:
    """Remove an account row. Creds/QR files are handled by core.delete_account."""
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM accounts WHERE id=?", (aid,))


# ------------------------------------------------------------------ contacts

def add_contacts(rows: list[dict]) -> int:
    n = 0
    with _LOCK, _conn() as c:
        for r in rows:
            phone = (r.get("phone") or "").strip()
            if not phone:
                continue
            try:
                c.execute(
                    "INSERT INTO contacts(phone, zalo_user_id, display_name, gender, tags, resolved, created_at)"
                    " VALUES(?,?,?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET"
                    " zalo_user_id=COALESCE(excluded.zalo_user_id, contacts.zalo_user_id),"
                    " display_name=COALESCE(NULLIF(excluded.display_name,''), contacts.display_name),"
                    " gender=CASE WHEN excluded.gender<>'' THEN excluded.gender ELSE contacts.gender END,"
                    " tags=CASE WHEN excluded.tags<>'' THEN excluded.tags ELSE contacts.tags END",
                    (phone, r.get("zalo_user_id"), r.get("display_name", ""),
                     (r.get("gender") or "").strip().lower(), r.get("tags", ""),
                     1 if r.get("zalo_user_id") else 0, int(time.time())))
                n += 1
            except sqlite3.Error:
                pass
    return n


def list_contacts(limit: int = 500) -> list[dict]:
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM contacts ORDER BY id DESC LIMIT ?", (limit,))]


def list_contacts_by_tags(tags: list[str], limit: int = 5000) -> list[dict]:
    """Contacts whose tag field matches ANY of the given tags (substring).
    Empty tag list -> all contacts."""
    tags = [t.strip() for t in (tags or []) if t and t.strip()]
    if not tags:
        return list_contacts(limit)
    with _LOCK, _conn() as c:
        clauses = " OR ".join(["tags LIKE ?"] * len(tags))
        params = [f"%{t}%" for t in tags] + [limit]
        return [dict(r) for r in c.execute(
            f"SELECT * FROM contacts WHERE ({clauses}) ORDER BY id DESC LIMIT ?", params)]


def all_tags() -> list[str]:
    """Distinct tags across contacts (split on commas), sorted."""
    out: set[str] = set()
    with _LOCK, _conn() as c:
        for (t,) in c.execute("SELECT tags FROM contacts WHERE tags IS NOT NULL AND tags<>''"):
            for part in str(t).split(","):
                part = part.strip()
                if part:
                    out.add(part)
    return sorted(out)


def set_campaign_tags(cid: str, tags: list[str]) -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE campaigns SET tags=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), cid))


def get_contact_by_phone(phone: str) -> dict | None:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM contacts WHERE phone=?", (phone,)).fetchone()
        return dict(r) if r else None


def update_contact_resolution(phone: str, zalo_user_id: str | None, display_name: str = "",
                              error: str = "", gender: str = "") -> None:
    """Upsert a resolved phone -> {userId, name, gender}. Creates the contact
    row when the phone is only present on a campaign target (not yet a contact),
    so the resolved display name is never dropped."""
    phone = (phone or "").strip()
    if not phone:
        return
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO contacts(phone, zalo_user_id, display_name, gender, resolved, last_error, created_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET"
            " zalo_user_id=COALESCE(excluded.zalo_user_id, contacts.zalo_user_id),"
            " display_name=COALESCE(NULLIF(excluded.display_name,''), contacts.display_name),"
            " gender=CASE WHEN excluded.gender<>'' THEN excluded.gender ELSE contacts.gender END,"
            " resolved=excluded.resolved, last_error=excluded.last_error",
            (phone, zalo_user_id, display_name, (gender or "").strip().lower(),
             1 if zalo_user_id else 0, error, int(time.time())))


# --------------------------------------------------------------- contact files

def create_contact_file(name: str, source: str = "", note: str = "") -> int:
    with _LOCK, _conn() as c:
        cur = c.execute("INSERT INTO contact_files(name,source,note,created_at) VALUES(?,?,?,?)",
                        (name.strip() or "Tệp KH", source, note, int(time.time())))
        return int(cur.lastrowid)


def list_contact_files() -> list[dict]:
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT f.*, (SELECT COUNT(*) FROM file_members m WHERE m.file_id=f.id) AS n"
            " FROM contact_files f ORDER BY f.id DESC")]


def get_contact_file(fid: int) -> dict | None:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM contact_files WHERE id=?", (fid,)).fetchone()
        return dict(r) if r else None


def rename_contact_file(fid: int, name: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE contact_files SET name=? WHERE id=?", (name.strip() or "Tệp KH", fid))


def delete_contact_file(fid: int) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM file_members WHERE file_id=?", (fid,))
        c.execute("DELETE FROM contact_files WHERE id=?", (fid,))


def add_file_members(fid: int, rows: list[dict]) -> int:
    """Add contacts to a file (creating the global contact row too). Returns count added."""
    n = 0
    with _LOCK, _conn() as c:
        for r in rows:
            phone = (r.get("phone") or "").strip()
            if not phone:
                continue
            c.execute(
                "INSERT INTO contacts(phone, zalo_user_id, display_name, gender, tags, resolved, created_at)"
                " VALUES(?,?,?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET"
                " zalo_user_id=COALESCE(excluded.zalo_user_id, contacts.zalo_user_id),"
                " display_name=COALESCE(NULLIF(excluded.display_name,''), contacts.display_name),"
                " gender=CASE WHEN excluded.gender<>'' THEN excluded.gender ELSE contacts.gender END",
                (phone, r.get("zalo_user_id"), r.get("display_name", ""),
                 (r.get("gender") or "").strip().lower(), r.get("tags", ""),
                 1 if r.get("zalo_user_id") else 0, int(time.time())))
            try:
                c.execute("INSERT OR IGNORE INTO file_members(file_id, phone) VALUES(?,?)", (fid, phone))
                n += 1
            except sqlite3.Error:
                pass
    return n


def list_file_members(fid: int, limit: int = 5000) -> list[dict]:
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT c.* FROM file_members m JOIN contacts c ON c.phone=m.phone"
            " WHERE m.file_id=? ORDER BY c.id LIMIT ?", (fid, limit))]


def remove_file_member(fid: int, phone: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM file_members WHERE file_id=? AND phone=?", (fid, phone))


def update_contact(phone: str, *, new_phone: str | None = None, display_name: str | None = None,
                   gender: str | None = None) -> None:
    """Edit a contact in place (phone change updates all file links)."""
    with _LOCK, _conn() as c:
        row = c.execute("SELECT * FROM contacts WHERE phone=?", (phone,)).fetchone()
        if not row:
            return
        sets, vals = [], []
        if display_name is not None:
            sets.append("display_name=?"); vals.append(display_name.strip())
        if gender is not None:
            sets.append("gender=?"); vals.append(gender.strip().lower())
        if new_phone:
            np = new_phone.strip()
            if np and np != phone:
                files = [r["file_id"] for r in c.execute("SELECT file_id FROM file_members WHERE phone=?", (phone,))]
                try:
                    c.execute("UPDATE contacts SET phone=? WHERE phone=?", (np, phone))
                    for f in files:
                        c.execute("INSERT OR IGNORE INTO file_members(file_id, phone) VALUES(?,?)", (f, np))
                    c.execute("DELETE FROM file_members WHERE phone=?", (phone,))
                except sqlite3.IntegrityError:
                    pass
        if sets:
            c.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE phone=?", (*vals, new_phone or phone))


def delete_contact(phone: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM file_members WHERE phone=?", (phone,))
        c.execute("DELETE FROM contacts WHERE phone=?", (phone,))


# ----------------------------------------------------------------- campaigns

def create_campaign(cid: str, **kw) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO campaigns(id,name,kind,account_id,thread_id,thread_type,body_template,url,"
            "media_path,images,blocks,status,dry_run,created_at,tags,stats)"
            " VALUES(:id,:name,:kind,:account_id,:thread_id,:thread_type,:body_template,:url,"
            ":media_path,:images,:blocks,'draft',:dry_run,:created_at,:tags,'{}')",
            {**{"id": cid, "name": "", "kind": "message", "account_id": None, "thread_id": None,
                "thread_type": "user", "body_template": "", "url": None, "media_path": None,
                "images": "[]", "blocks": "[]", "dry_run": 1, "created_at": int(time.time()),
                "tags": "[]"}, **kw})


def get_campaign(cid: str) -> dict | None:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None


def list_campaigns(limit: int = 100) -> list[dict]:
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM campaigns ORDER BY created_at DESC LIMIT ?", (limit,))]


def set_campaign_status(cid: str, status: str, **kw) -> None:
    with _LOCK, _conn() as c:
        sets = ["status=?"]
        vals: list = [status]
        for k, v in kw.items():
            sets.append(f"{k}=?"); vals.append(v)
        vals.append(cid)
        c.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE id=?", vals)


def set_campaign_stats(cid: str, stats: dict) -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE campaigns SET stats=? WHERE id=?", (json.dumps(stats), cid))


def set_campaign_dry(cid: str, dry: int) -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE campaigns SET dry_run=? WHERE id=?", (1 if dry else 0, cid))


_CAMP_FIELDS = {"name", "kind", "account_id", "thread_id", "thread_type",
                "body_template", "url", "images", "tags", "dry_run", "blocks"}


def update_campaign(cid: str, **kw) -> int:
    """Update editable campaign fields (whitelist). Returns rows changed."""
    fields = {k: v for k, v in kw.items() if k in _CAMP_FIELDS and v is not None}
    if not fields:
        return 0
    with _LOCK, _conn() as c:
        sets = ", ".join(f"{k}=?" for k in fields)
        cur = c.execute(f"UPDATE campaigns SET {sets} WHERE id=?", [*fields.values(), cid])
        return cur.rowcount


def set_campaign_images(cid: str, paths: list[str]) -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE campaigns SET images=? WHERE id=?",
                  (json.dumps(list(paths or []), ensure_ascii=False), cid))


def delete_campaign(cid: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM targets WHERE campaign_id=?", (cid,))
        c.execute("DELETE FROM campaigns WHERE id=?", (cid,))
        c.execute("DELETE FROM events WHERE campaign_id=?", (cid,))


def clear_targets(cid: str) -> int:
    """Remove all targets of a campaign (used when rebuilding the list)."""
    with _LOCK, _conn() as c:
        cur = c.execute("DELETE FROM targets WHERE campaign_id=?", (cid,))
        return cur.rowcount


def trim_targets(cid: str, keep: int = 1) -> int:
    """Keep only the first `keep` targets (by id) and delete the rest. Used for
    group-broadcast campaigns where every target maps to the same group thread."""
    with _LOCK, _conn() as c:
        ids = [r[0] for r in c.execute(
            "SELECT id FROM targets WHERE campaign_id=? ORDER BY id", (cid,))]
        drop = ids[keep:]
        for tid in drop:
            c.execute("DELETE FROM targets WHERE id=?", (tid,))
        return len(drop)


# --------------------------------------------------------------- phone identity

def canon_phone(s) -> str:
    """Canonical phone so '0912345678' / '+84912345678' / '84912345678' are ONE
    key. Non-phone values (e.g. a Zalo userId living in the phone column) are
    returned digits-only so they still de-dupe consistently."""
    d = "".join(ch for ch in str(s or "") if ch.isdigit())
    if not d:
        return ""
    # 84 + 9-digit VN number = 11..12 digits; anything longer is a userId, not a
    # phone, so never rewrite it (e.g. a 19-digit id starting with '84').
    if d.startswith("84") and 11 <= len(d) <= 12:
        return "0" + d[2:]
    return d


def canon_ident(phone, zalo_uid) -> str:
    """Identity for cross-campaign de-dup: a real Zalo userId wins over phone."""
    uid = "".join(ch for ch in str(zalo_uid or "") if ch.isdigit())
    if uid and len(uid) >= 8:
        return "u:" + uid
    return "p:" + canon_phone(phone)


def add_targets(cid: str, rows: list[dict]) -> int:
    n = 0
    with _LOCK, _conn() as c:
        for r in rows:
            raw = (r.get("phone") or "").strip()
            if not raw:
                continue
            # Canonicalise so the same person can't be targeted twice under a
            # different phone spelling (F3: '0912...' vs '+84912...').
            phone = canon_phone(raw) or raw
            uid = (r.get("zalo_user_id") or "").strip()
            ident = canon_ident(phone, uid)
            try:
                cur = c.execute(
                    "INSERT INTO targets(campaign_id, contact_id, phone, zalo_user_id, ident, status, updated_at)"
                    " VALUES(?,?,?,?,?,'pending',?) ON CONFLICT(campaign_id, phone) DO NOTHING",
                    (cid, r.get("contact_id"), phone, uid or None, ident, int(time.time())))
                # only count rows actually written (ON CONFLICT skips are not adds)
                if cur.rowcount:
                    n += 1
            except sqlite3.Error:
                pass
    return n


def sent_ident_recent(account_id: str, ident: str, days: int = 3) -> bool:
    """True when this identity was already SENT in a LIVE campaign in the last
    `days`. Stops the same person being messaged again across campaigns (F4).
    DRY-RUN sends never count."""
    if not ident or len(str(ident)) < 3:
        return False
    since = int(time.time()) - int(days) * 86400
    with _LOCK, _conn() as c:
        r = c.execute(
            "SELECT 1 FROM targets t JOIN campaigns cp ON cp.id=t.campaign_id"
            " WHERE cp.account_id=? AND t.ident=? AND t.status='sent' AND cp.dry_run=0"
            " AND t.updated_at>=? LIMIT 1",
            (account_id, ident, since)).fetchone()
    return bool(r)


def pending_targets(cid: str, limit: int = 1000) -> list[dict]:
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM targets WHERE campaign_id=? AND status='pending' ORDER BY id LIMIT ?",
            (cid, limit))]


def display_name_for(value: str) -> str:
    """Best-effort: a target's phone OR zalo_user_id -> a known display name
    (from contacts, else from a previously-scanned group member)."""
    v = (value or "").strip()
    if not v:
        return ""
    with _LOCK, _conn() as c:
        r = c.execute("SELECT display_name FROM contacts WHERE phone=? AND display_name<>'' LIMIT 1", (v,)).fetchone()
        if r and r["display_name"]:
            return r["display_name"]
        r = c.execute("SELECT display_name FROM contacts WHERE zalo_user_id=? AND display_name<>'' LIMIT 1", (v,)).fetchone()
        if r and r["display_name"]:
            return r["display_name"]
        r = c.execute("SELECT display_name FROM members WHERE user_id=? AND display_name<>'' LIMIT 1", (v,)).fetchone()
        if r and r["display_name"]:
            return r["display_name"]
    return ""


def mark_target(tid: int, status: str, error: str = "", msg_id: str = "") -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE targets SET status=?, error=?, msg_id=?, updated_at=? WHERE id=?",
                  (status, error, msg_id, int(time.time()), tid))


def set_target_resolution(cid: str, phone: str, zalo_user_id: str) -> None:
    """Store a resolved Zalo user id on a campaign target (phone -> userId).
    Also refreshes the canonical `ident` so cross-campaign de-dup keys on the
    real userId once known."""
    uid = "".join(ch for ch in str(zalo_user_id or "") if ch.isdigit())
    ident = ("u:" + uid) if (uid and len(uid) >= 8) else ("p:" + canon_phone(phone))
    with _LOCK, _conn() as c:
        c.execute("UPDATE targets SET zalo_user_id=?, ident=? WHERE campaign_id=? AND phone=?",
                  (zalo_user_id, ident, cid, phone))
        # also update any target that stored the userId in the phone column
        c.execute("UPDATE targets SET zalo_user_id=?, ident=? WHERE campaign_id=? AND phone=?",
                  (zalo_user_id, ident, cid, zalo_user_id))


def targets_for(cid: str, limit: int = 1000) -> list[dict]:
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM targets WHERE campaign_id=? ORDER BY id LIMIT ?", (cid, limit))]


def get_target(tid: int) -> dict | None:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM targets WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None


def skip_pending_targets(cid: str, reason: str = "skipped") -> int:
    """Mark all still-pending targets of a campaign as skipped; return count."""
    with _LOCK, _conn() as c:
        cur = c.execute(
            "UPDATE targets SET status='skipped', error=?, updated_at=? "
            "WHERE campaign_id=? AND status='pending'",
            (reason, int(time.time()), cid))
        return cur.rowcount


def reset_targets(cid: str, statuses: list[str]) -> int:
    """Move targets in the given statuses back to 'pending' (for re-run)."""
    if not statuses:
        return 0
    ph = ",".join("?" for _ in statuses)
    with _LOCK, _conn() as c:
        cur = c.execute(
            f"UPDATE targets SET status='pending', error='', msg_id='', updated_at=? "
            f"WHERE campaign_id=? AND status IN ({ph})",
            (int(time.time()), cid, *statuses))
        return cur.rowcount


def campaign_counts(cid: str) -> dict:
    with _LOCK, _conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM targets WHERE campaign_id=? GROUP BY status", (cid,)).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    total = sum(counts.values())
    return {"total": total, **counts}


# -------------------------------------------------------------------- groups

def upsert_groups(account_id: str, rows: list[dict]) -> int:
    n = 0
    with _LOCK, _conn() as c:
        for g in rows:
            if not g.get("groupId"):
                continue
            c.execute(
                "INSERT INTO groups(account_id,group_id,name,total_member,member_count,admin_count,avt,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_id,group_id) DO UPDATE SET"
                " name=excluded.name, total_member=excluded.total_member, member_count=excluded.member_count,"
                " admin_count=excluded.admin_count, avt=excluded.avt, updated_at=excluded.updated_at",
                (account_id, g["groupId"], g.get("name", ""), g.get("totalMember", 0),
                 g.get("memberCount", 0), g.get("adminCount", 0), g.get("avt", ""), int(time.time())))
            n += 1
    return n


def list_groups(account_id: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK, _conn() as c:
        if account_id:
            rows = c.execute("SELECT * FROM groups WHERE account_id=? ORDER BY total_member DESC LIMIT ?",
                             (account_id, limit))
        else:
            rows = c.execute("SELECT * FROM groups ORDER BY updated_at DESC, total_member DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def upsert_members(account_id: str, group_id: str, members: list[dict]) -> int:
    n = 0
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM members WHERE group_id=?", (group_id,))
        for m in members:
            if not m.get("userId"):
                continue
            c.execute(
                "INSERT INTO members(account_id,group_id,user_id,display_name,global_id,is_admin,scanned_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (account_id, group_id, m["userId"], m.get("displayName", ""),
                 m.get("globalId", ""), 1 if m.get("isAdmin") else 0, int(time.time())))
            n += 1
    return n


def list_members(group_id: str, limit: int = 2000) -> list[dict]:
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM members WHERE group_id=? ORDER BY is_admin DESC, id LIMIT ?", (group_id, limit))]


# -------------------------------------------------------------------- events

def log_event(kind: str, detail: str = "", account_id: str | None = None, campaign_id: str | None = None) -> None:
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO events(ts,account_id,campaign_id,kind,detail) VALUES(?,?,?,?,?)",
                  (int(time.time()), account_id, campaign_id, kind, detail[:2000]))


def recent_events(limit: int = 100) -> list[dict]:
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


# ------------------------------------------------------------------ settings

def get_setting(key: str, default=None):
    with _LOCK, _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not r:
            return default
        try:
            return json.loads(r["value"])
        except Exception:
            return r["value"]

def set_setting(key: str, value) -> None:
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))


# --------------------------------------------------------------- rate limiter

def count_sent_since(account_id: str, since_ts: int, kind: str | None = None,
                     live_only: bool = True) -> int:
    """Count 'sent' targets for an account since a timestamp.

    live_only (default True) excludes DRY-RUN campaigns so test sends never eat
    into the real anti-ban rate budget. Pass live_only=False for a pure total.
    """
    dry_clause = " AND cp.dry_run=0" if live_only else ""
    with _LOCK, _conn() as c:
        if kind:
            r = c.execute(
                "SELECT COUNT(*) n FROM targets t JOIN campaigns cp ON cp.id=t.campaign_id"
                " WHERE cp.account_id=? AND t.status='sent' AND t.updated_at>=? AND cp.kind=?"
                + dry_clause,
                (account_id, since_ts, kind)).fetchone()
        else:
            r = c.execute(
                "SELECT COUNT(*) n FROM targets t JOIN campaigns cp ON cp.id=t.campaign_id"
                " WHERE cp.account_id=? AND t.status='sent' AND t.updated_at>=?"
                + dry_clause,
                (account_id, since_ts)).fetchone()
    return r["n"] if r else 0


def last_sent_ts(account_id: str, live_only: bool = True) -> int:
    """Timestamp of the last 'sent' target. live_only (default) ignores DRY-RUN
    sends so the min-gap throttle and 'last sent' UI reflect REAL traffic."""
    dry_clause = " AND cp.dry_run=0" if live_only else ""
    with _LOCK, _conn() as c:
        r = c.execute(
            "SELECT MAX(t.updated_at) m FROM targets t JOIN campaigns cp ON cp.id=t.campaign_id"
            " WHERE cp.account_id=? AND t.status='sent'" + dry_clause, (account_id,)).fetchone()
    return (r["m"] or 0) if r else 0


# ------------------------------------------------------- scan rate tracking

def scan_counts(account_id: str, since_ts: int) -> int:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT COUNT(*) n FROM events WHERE account_id=? AND kind='group_scan' AND ts>=?",
                      (account_id, since_ts)).fetchone()
    return r["n"] if r else 0


def last_scan_ts(account_id: str) -> int:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT MAX(ts) m FROM events WHERE account_id=? AND kind='group_scan'",
                      (account_id,)).fetchone()
    return (r["m"] or 0) if r else 0


# ------------------------------------------------ group scan progress (live)

def scan_progress_start(group_id: str, account_id: str, name: str = "") -> None:
    now = int(time.time())
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO group_scan_progress(group_id,account_id,name,status,total,fetched,partial,error,started_at,updated_at,finished_at)"
            " VALUES(?,?,?,'running',0,0,0,'',?,?,NULL)"
            " ON CONFLICT(group_id) DO UPDATE SET account_id=excluded.account_id, name=excluded.name,"
            " status='running', total=0, fetched=0, partial=0, error='', started_at=excluded.started_at,"
            " updated_at=excluded.updated_at, finished_at=NULL",
            (group_id, account_id, name, now, now))


def scan_progress_update(group_id: str, *, total: int | None = None, fetched: int | None = None,
                         partial: int | None = None, error: str | None = None) -> None:
    sets, vals = [], []
    if total is not None:
        sets.append("total=?"); vals.append(total)
    if fetched is not None:
        sets.append("fetched=?"); vals.append(fetched)
    if partial is not None:
        sets.append("partial=?"); vals.append(partial)
    if error is not None:
        sets.append("error=?"); vals.append(error)
    sets.append("updated_at=?"); vals.append(int(time.time()))
    with _LOCK, _conn() as c:
        c.execute(f"UPDATE group_scan_progress SET {', '.join(sets)} WHERE group_id=?", (*vals, group_id))


def scan_progress_finish(group_id: str, *, status: str = "done", total: int = 0, fetched: int = 0,
                         partial: int = 0, error: str = "") -> None:
    now = int(time.time())
    with _LOCK, _conn() as c:
        c.execute(
            "UPDATE group_scan_progress SET status=?, total=?, fetched=?, partial=?, error=?,"
            " updated_at=?, finished_at=? WHERE group_id=?",
            (status, total, fetched, partial, error[:500], now, now, group_id))


def scan_progress_get(group_id: str) -> dict | None:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM group_scan_progress WHERE group_id=?", (group_id,)).fetchone()
    return dict(r) if r else None


def scan_progress_recent(limit: int = 50) -> list[dict]:
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT p.*, g.name AS group_name, g.total_member FROM group_scan_progress p"
            " LEFT JOIN groups g ON g.group_id=p.group_id"
            " ORDER BY p.updated_at DESC LIMIT ?", (limit,))]


# ------------------------------------------------------------ autobot (forward)
_FWD_FIELDS = {"name", "account_id", "src_group_id", "src_group_name", "dst_group_id",
               "dst_group_name", "enabled", "dry_run", "include_self", "prefix",
               "max_per_hour", "gap_seconds", "group_window_ms",
               "reply_mode", "reply_quote_ms", "last_seen_msg_id",
               "last_run_at", "forwarded_count", "skipped_count", "error"}


def create_forward_rule(rid: str, **kw) -> None:
    now = int(time.time())
    defaults = {"id": rid, "name": "", "account_id": None, "src_group_id": None,
                "src_group_name": "", "dst_group_id": None, "dst_group_name": "",
                "enabled": 1, "dry_run": 1, "include_self": 0, "prefix": "",
                "max_per_hour": 60, "gap_seconds": 5, "group_window_ms": 2000,
                "reply_mode": "text", "reply_quote_ms": 0,
                "last_seen_msg_id": "",
                "last_run_at": None, "forwarded_count": 0, "skipped_count": 0,
                "error": "", "created_at": now, "updated_at": now}
    row = {**defaults, **{k: v for k, v in kw.items() if k in _FWD_FIELDS or k == "id"}}
    cols = ",".join(row.keys())
    ph = ",".join(":" + k for k in row.keys())
    with _LOCK, _conn() as c:
        c.execute(f"INSERT INTO forward_rules({cols}) VALUES({ph})", row)


def get_forward_rule(rid: str) -> dict | None:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM forward_rules WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None


def list_forward_rules() -> list[dict]:
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM forward_rules ORDER BY created_at DESC")]


def find_forward_by_src(rule_id: str, src_group_id: str, src_msg_id: str) -> dict | None:
    """Look up the forwarded record for a source message (used to map a reply's
    parent to its copy in the destination group)."""
    if not src_msg_id:
        return None
    with _LOCK, _conn() as c:
        r = c.execute(
            "SELECT * FROM forward_log WHERE rule_id=? AND src_group_id=? AND src_msg_id=? "
            "ORDER BY id DESC LIMIT 1", (rule_id, src_group_id, src_msg_id)).fetchone()
    return dict(r) if r else None


def last_forward_src_msg_id(rule_id: str, src_group_id: str) -> str:
    """Newest src_msg_id already logged for a rule+group (history backfill
    cursor fallback when a rule has no `last_seen_msg_id` yet)."""
    with _LOCK, _conn() as c:
        r = c.execute(
            "SELECT src_msg_id FROM forward_log WHERE rule_id=? AND src_group_id=? "
            "AND src_msg_id<>'' ORDER BY id DESC LIMIT 1", (rule_id, src_group_id)).fetchone()
    return str(r["src_msg_id"]) if r else ""


def msgzilla_lookup(account_id: str, cli_msg_id: str) -> dict | None:
    """Map a Zalo cliMsgId we POSTed to the msgId Zalo assigned it (msgId+1).
    Optional: only usable once an outbound-message table exists. Returning None
    simply means the caller falls back to the text/reply context block."""
    cid = str(cli_msg_id or "").strip()
    if not cid:
        return None
    with _LOCK, _conn() as c:
        try:
            r = c.execute(
                "SELECT * FROM msgzilla WHERE account_id=? AND cli_msg_id=? LIMIT 1",
                (str(account_id or ""), cid)).fetchone()
        except Exception:
            return None
    return dict(r) if r else None


def update_forward_rule(rid: str, **kw) -> int:
    kw = {k: v for k, v in kw.items() if k in _FWD_FIELDS}
    if not kw:
        return 0
    kw["updated_at"] = int(time.time())
    sets = ", ".join(f"{k}=:{k}" for k in kw)
    with _LOCK, _conn() as c:
        cur = c.execute(f"UPDATE forward_rules SET {sets} WHERE id=:id", {**kw, "id": rid})
        return cur.rowcount


def delete_forward_rule(rid: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM forward_rules WHERE id=?", (rid,))
        c.execute("DELETE FROM forward_log WHERE rule_id=?", (rid,))


def bump_forward_rule(rid: str, *, forwarded: int = 0, skipped: int = 0,
                      last_seen_msg_id: str | None = None, error: str | None = None) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "UPDATE forward_rules SET forwarded_count=forwarded_count+?, skipped_count=skipped_count+?,"
            " last_seen_msg_id=COALESCE(?, last_seen_msg_id), error=COALESCE(?, error),"
            " last_run_at=?, updated_at=? WHERE id=?",
            (forwarded, skipped, last_seen_msg_id, error, int(time.time()), int(time.time()), rid))


def add_forward_log(rule_id: str, src_group_id: str, src_name: str, text: str,
                    status: str, dst_msg_id: str = "", src_msg_id: str = "",
                    error: str = "") -> bool:
    """Append a forward log row. Returns True if written, False if it was a
    duplicate of an existing (rule_id, src_group_id, src_msg_id) row."""
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO forward_log(rule_id,src_group_id,src_msg_id,src_name,text,status,dst_msg_id,error,ts)"
            " VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(rule_id,src_group_id,src_msg_id) WHERE src_msg_id<>'' DO NOTHING",
            (rule_id, src_group_id, str(src_msg_id or ""), (src_name or "")[:120],
             (text or "")[:2000], status, str(dst_msg_id or ""), str(error or "")[:500],
             int(time.time())))
        if cur.rowcount == 0:
            return False
        # keep the per-rule log bounded
        c.execute("DELETE FROM forward_log WHERE id NOT IN ("
                  "SELECT id FROM forward_log ORDER BY id DESC LIMIT 5000)")
        return True


# ------------------------------------------------------- forward dedup ledger

def seen_forward(account_id: str, group_id: str, msg_key: str, ttl_seconds: int = 2592000) -> bool:
    """Atomic check-and-set in the durable ledger.

    Returns True if this (account, group, msg_key) is NEW (caller may forward),
    False if it was seen before. Persists across restarts, so a message replayed
    when the listener reconnects is not forwarded twice.

    TTL now defaults to 30 days (was 24h). The 24h prune silently evicted rows a
    day old, which made a reconnect backfill re-deliver anything older than 24h
    and made "the ledger says seen" unreliable for long gaps.
    """
    now = int(time.time())
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO forward_seen(account_id,group_id,msg_key,ts) VALUES(?,?,?,?)"
            " ON CONFLICT(account_id,group_id,msg_key) DO NOTHING",
            (account_id, str(group_id or ""), msg_key, now))
        fresh = cur.rowcount == 1
        # opportunistic prune so the ledger stays bounded
        c.execute("DELETE FROM forward_seen WHERE ts < ?", (now - int(ttl_seconds or 86400),))
        return fresh


def seen_recent_count(account_id: str, group_id: str) -> int:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT COUNT(*) FROM forward_seen WHERE account_id=? AND group_id=?",
                      (account_id, str(group_id or ""))).fetchone()
    return int(r[0]) if r else 0


def list_forward_log(limit: int = 200, rule_id: str | None = None) -> list[dict]:
    with _LOCK, _conn() as c:
        if rule_id:
            rows = c.execute("SELECT * FROM forward_log WHERE rule_id=? ORDER BY id DESC LIMIT ?",
                             (rule_id, limit))
        else:
            rows = c.execute("SELECT * FROM forward_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def count_forwarded_since(rule_id: str, since_ts: int) -> int:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT COUNT(*) FROM forward_log WHERE rule_id=? AND status='sent' AND ts>=?",
                      (rule_id, since_ts)).fetchone()
    return int(r[0]) if r else 0
