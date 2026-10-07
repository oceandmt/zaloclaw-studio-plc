"""Core engine for zaloclaw-studio: bridge calls, rate-limited campaign worker,
QR login orchestration.

Safety model:
  * Everything defaults to DRY-RUN unless a campaign/account explicitly opts in.
  * A global worker enforces per-account hourly/daily caps + min-gap + jitter.
  * A KILLSWITCH file pauses all live sending immediately.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import random
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

import db
import sessiond_client

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "settings.json"
NODE = os.environ.get("ZS_NODE", "node")
# Runtime role. One codebase, two processes, so each engine has exactly one owner
# and debugging one pipeline can never touch the other:
#   all      -> both engines (default; single-app/test mode)
#   campaign -> campaign worker only (G2G listeners NOT started here)
#   g2g      -> G2G pipeline only (campaign worker NOT started here)
# Both roles share ONE per-account session daemon, so there is never a second
# Zalo session on the same account.
ROLE = (os.environ.get("ZS_ROLE", "all").strip() or "all").lower()


def role_allows_campaign() -> bool:
    return ROLE in ("all", "campaign")


def role_allows_g2g() -> bool:
    return ROLE in ("all", "g2g")
BRIDGE = ROOT / "bridge" / "zalo.mjs"
ACCOUNTS_DIR = ROOT / "data" / "accounts"
MEDIA_DIR = ROOT / "data" / "media"
KILLSWITCH = ROOT / "data" / "KILLSWITCH"

_LOG: list[str] = []
_LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    with _LOG_LOCK:
        _LOG.append(line)
        del _LOG[:-500]


def logs(n: int = 200, newest_first: bool = True) -> list[str]:
    with _LOG_LOCK:
        lines = list(_LOG[-n:])
    # newest at the top by default so the freshest events are visible first
    return list(reversed(lines)) if newest_first else lines


def settings() -> dict:
    cfg = {"defaults": {}, "safety": {}, "bridge": {}}
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text())
    cfg["defaults"] = {**cfg.get("defaults", {}), **(db.get_setting("defaults") or {})}
    return cfg


def killswitch_active() -> bool:
    return KILLSWITCH.exists()


def set_killswitch(on: bool) -> None:
    if on:
        KILLSWITCH.write_text(f"on {time.ctime()}\n")
    elif KILLSWITCH.exists():
        KILLSWITCH.unlink()


_RATE_KEYS = {
    "max_per_hour": int, "max_per_day": int, "max_friend_per_day": int,
    "min_gap_seconds": int, "jitter_seconds": int,
    "group_scan_per_hour": int, "group_scan_per_day": int,
    "group_scan_gap_seconds": int, "group_scan_jitter_seconds": int,
    "group_sync_gap_seconds": int, "dedupe_sent_days": int,
}

# (min, max) clamps so a fat-fingered 0/negative/1e9 can't silently disable the
# anti-ban throttle (e.g. max_per_hour=0 would block all LIVE sends) or unmask
# the account (e.g. min_gap_seconds=0).
_RATE_CLAMPS = {
    "max_per_hour": (1, 100000), "max_per_day": (1, 1000000),
    "max_friend_per_day": (1, 10000),
    "min_gap_seconds": (0, 86400), "jitter_seconds": (0, 86400),
    "group_scan_per_hour": (1, 100000), "group_scan_per_day": (1, 1000000),
    "group_scan_gap_seconds": (0, 86400), "group_scan_jitter_seconds": (0, 86400),
    "group_sync_gap_seconds": (0, 86400), "dedupe_sent_days": (0, 365),
}


def _clamp(k: str, v: int) -> int:
    lo, hi = _RATE_CLAMPS.get(k, (None, None))
    if lo is not None:
        v = max(lo, min(hi, v))
    return v


# --------------------------------------------------------- quiet hours
# Operator can pick a daily window during which NO live message/friend sends
# leave (anti-ban at night, off-hours, ...). Times are local "HH:MM" strings.
# A window may cross midnight (e.g. 22:00 -> 06:00).

def _hhmm_to_min(value) -> int | None:
    """Parse 'HH:MM' / 'H:MM' (or 'HHMM') -> minutes since midnight, else None."""
    s = str(value or "").strip()
    m = re.fullmatch(r"(\d{1,2})\s*:\s*(\d{1,2})", s) or re.fullmatch(r"(\d{2})(\d{2})", s)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh * 60 + mm


def _min_to_hhmm(v: int) -> str:
    return f"{(v // 60) % 24:02d}:{v % 60:02d}"


def quiet_window(defaults: dict) -> tuple[int, int] | None:
    """Return (from_min, to_min) when quiet hours are armed and valid, else None."""
    if not defaults.get("quiet_hours_enabled"):
        return None
    a = _hhmm_to_min(defaults.get("quiet_from"))
    b = _hhmm_to_min(defaults.get("quiet_to"))
    if a is None or b is None or a == b:
        return None  # unset / equal -> window disabled (never silence 24/7)
    return a, b


def _now_min(now: float | None = None) -> int:
    t = time.localtime(now if now is not None else time.time())
    return t.tm_hour * 60 + t.tm_min


def in_quiet_hours(defaults: dict, now: float | None = None) -> bool:
    w = quiet_window(defaults)
    if not w:
        return False
    a, b = w
    cur = _now_min(now)
    return (a < cur < b) if a < b else (cur > a or cur < b)


def quiet_seconds_left(defaults: dict, now: float | None = None) -> int:
    """Seconds until the current quiet window ends (0 when not quiet)."""
    if not in_quiet_hours(defaults, now):
        return 0
    a, b = quiet_window(defaults)  # type: ignore[misc]
    cur = _now_min(now)
    mins = (b - cur) if (a < b) else ((1440 - cur) + b)
    return max(0, mins) * 60


def quiet_status(defaults: dict | None = None) -> dict:
    """Compact quiet-hours state for the UI header/warnings."""
    d = defaults if defaults is not None else settings().get("defaults", {})
    w = quiet_window(d)
    active = in_quiet_hours(d)
    return {
        "enabled": bool(d.get("quiet_hours_enabled")),
        "from": str(d.get("quiet_from") or ""),
        "to": str(d.get("quiet_to") or ""),
        "active": active,
        "until": (_min_to_hhmm(quiet_window(d)[1]) if (w and active) else ""),
        "left": quiet_seconds_left(d) if active else 0,
    }


def save_rate_settings(form: dict) -> dict:
    """Persist rate-limit overrides (stored in db settings under 'defaults')."""
    cur = db.get_setting("defaults") or {}
    changed = {}
    for k, cast in _RATE_KEYS.items():
        if k in form and str(form[k]).strip() != "":
            try:
                changed[k] = _clamp(k, cast(str(form[k]).strip()))
            except (TypeError, ValueError):
                pass
    if changed:
        db.set_setting("defaults", {**cur, **changed})
        log(f"settings updated :: {changed}")
    # boolean toggles (checkboxes). A hidden companion field of the same name
    # (value 0) is submitted first so the LAST value wins when unticked.
    for bkey in ("auto_resolve_contacts", "set_alias_on_send", "quiet_hours_enabled"):
        if bkey in form:
            raw = str(form.get(bkey, ""))
            parts = [p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()]
            val = bool(parts) and parts[-1] in ("1", "on", "true", "yes")
            cur = db.get_setting("defaults") or {}
            db.set_setting("defaults", {**cur, bkey: val})
            log(f"settings updated :: {bkey}={val}")
    # quiet-hours window (local "HH:MM"); invalid values are ignored so a bad
    # submit can never widen/narrow the window accidentally.
    for tkey in ("quiet_from", "quiet_to"):
        if tkey in form and str(form.get(tkey, "")).strip() != "":
            v = _hhmm_to_min(form.get(tkey))
            if v is not None:
                cur = db.get_setting("defaults") or {}
                db.set_setting("defaults", {**cur, tkey: _min_to_hhmm(v)})
                log(f"settings updated :: {tkey}={_min_to_hhmm(v)}")
    return {**cur, **changed}


def _looks_like_userid(value: str) -> bool:
    """True when `value` is a Zalo USER ID rather than a phone number.

    Group-derived contacts store the member's userId in the `phone` column
    (e.g. '1292846114859475834'), so the phone-lookup API can't resolve them.
    We reuse the same VN-phone test the campaign resolver uses: anything with
    enough digits that is NOT a real phone is treated as a userId.
    """
    d = re.sub(r"\D", "", value or "")
    return len(d) >= 8 and not is_real_phone(value)


def resolve_phones(account_id: str, phones: list[str], cid: str | None = None,
                   chunk: int = 50) -> int:
    """Resolve a specific list of phone numbers to Zalo userId + name + gender.
    When cid is given, also stamp the resolved id onto that campaign's targets.
    Used by the campaign start path so it only touches THIS campaign's phones."""
    uniq = [p for p in dict.fromkeys(p.strip() for p in phones) if p and is_real_phone(p)]
    resolved = 0
    for part in [uniq[i:i + chunk] for i in range(0, len(uniq), chunk)]:
        r = sessiond_client.cmd(account_id, {"cmd": "find-users", "phones": part}, timeout=180)
        if r.get("ok"):
            for u in r.get("users", []):
                if u.get("phone"):
                    db.update_contact_resolution(u["phone"], u.get("userId"),
                                                 u.get("displayName", ""), gender=u.get("gender", ""))
                    if cid and u.get("userId"):
                        db.set_target_resolution(cid, u["phone"], u.get("userId"))
                    resolved += 1
        time.sleep(1)
    # find-users only returns phones it can resolve; fall back to the contacts
    # address book for the rest so targets whose phone is already known are
    # still stamped with a Zalo userId (otherwise the send path falls back to a
    # raw phone thread -> Zalo "Tham số không hợp lệ" retry loop).
    if cid:
        cmap = {c["phone"]: c.get("zalo_user_id") for c in db.list_contacts(5000) if c.get("phone")}
        for p in uniq:
            zid = cmap.get(p)
            if zid:
                db.set_target_resolution(cid, p, zid)
    return resolved


def resolve_contacts(account_id: str = "default", only_missing: bool = True, limit: int = 500,
                     file_id: int | None = None) -> dict:
    """Look each contact up on Zalo to fill display_name + gender.

    Phone contacts are resolved via `find-users --phones`; group-derived
    contacts (whose `phone` is really a userId) via `user-info --user-ids`.
    Useful when a customer file only has phone numbers / group member ids.
    If file_id is given, only members of that file are resolved."""
    if not is_logged_in(account_id):
        return {"ok": False, "error": "not logged in"}
    contacts = db.list_file_members(file_id) if file_id else db.list_contacts(5000)
    rows = [c for c in contacts if c.get("phone") and (not only_missing or not c.get("display_name") or not c.get("gender"))]
    ids = [c["phone"] for c in rows if _looks_like_userid(c["phone"])][:limit]
    phones = [c["phone"] for c in rows if not _looks_like_userid(c["phone"])][:limit]
    if not ids and not phones:
        return {"ok": True, "resolved": 0, "total": 0, "message": "không có SĐT cần tra"}
    resolved = 0
    # 1) Group-member contacts -> lookup by Zalo userId (fills gender).
    for chunk in [ids[i:i + 40] for i in range(0, len(ids), 40)]:
        r = sessiond_client.cmd(account_id, {"cmd": "user-info", "userIds": chunk}, timeout=180)
        if r.get("ok"):
            for u in r.get("users", []):
                key = u.get("userId")
                if key:
                    db.update_contact_resolution(key, key, u.get("displayName", ""),
                                                 gender=u.get("gender", ""))
                    resolved += 1
        time.sleep(1)
    # 2) Phone contacts -> lookup by phone number.
    for chunk in [phones[i:i + 50] for i in range(0, len(phones), 50)]:
        r = sessiond_client.cmd(account_id, {"cmd": "find-users", "phones": chunk}, timeout=180)
        if r.get("ok"):
            for u in r.get("users", []):
                if u.get("phone"):
                    db.update_contact_resolution(u["phone"], u.get("userId"),
                                                 u.get("displayName", ""), gender=u.get("gender", ""))
                    resolved += 1
        time.sleep(1)
    total = len(ids) + len(phones)
    db.log_event("contacts_resolve", f"{resolved}/{total} đã tra tên/giới tính", account_id=account_id)
    log(f"RESOLVE contacts :: {resolved}/{total}")
    return {"ok": True, "resolved": resolved, "total": total,
            "ids": len(ids), "phones": len(phones)}


# ------------------------------------------------------------------- bridge

def _safe_cmd(account_id: str, obj: dict, timeout: float = 120.0) -> dict:
    """Send a command to the shared session daemon; never raise."""
    if not is_logged_in(account_id):
        return {"ok": False, "error": "not logged in"}
    try:
        return sessiond_client.cmd(account_id, obj, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"sessiond: {e}"}


def bridge_stream(args: list[str], on_event, timeout: int = 900) -> dict:
    """Run a bridge command that streams NDJSON lines on stdout; invoke on_event(dict)
    for each parsed line. Returns the final 'done' event (or an error dict)."""
    cmd = [NODE, str(BRIDGE), *args]
    final: dict = {"ok": False, "error": "no output"}
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e)}
    deadline = time.time() + timeout
    try:
        for line in p.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            final = ev
            try:
                on_event(ev)
            except Exception as e:  # pragma: no cover
                log(f"stream callback error: {e}")
            if time.time() > deadline:
                p.kill()
                return {"ok": False, "error": "timeout"}
    finally:
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
    if not final.get("ok"):
        err = ""
        try:
            err = (p.stderr.read() or "").strip()[-300:]  # type: ignore[union-attr]
        except Exception:
            pass
        final.setdefault("error", err or "bridge failed")
    return final


# ------------------------------------------------------------- QR login flow

_qr_procs: dict[str, subprocess.Popen] = {}


def account_creds_path(account_id: str) -> Path:
    return ACCOUNTS_DIR / f"{account_id}.json"


def is_logged_in(account_id: str) -> bool:
    return account_creds_path(account_id).exists()


def account_enabled(account_id: str) -> bool:
    """An account is enabled unless explicitly turned off (default ON)."""
    a = db.get_account(account_id)
    if not a:
        return True
    return bool(a.get("enabled", 1))


def account_online(account_id: str) -> bool:
    """True when the account is enabled AND has stored credentials."""
    return account_enabled(account_id) and is_logged_in(account_id)


def start_qr_login(account_id: str) -> Path:
    """Spawn `login-qr` in background; stream ndjson to data/accounts/<id>.qr.log."""
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    emit = ACCOUNTS_DIR / f"{account_id}.qr.log"
    emit.write_text("")
    # stop any previous attempt for this account
    prev = _qr_procs.pop(account_id, None)
    if prev and prev.poll() is None:
        prev.terminate()
    p = subprocess.Popen(
        [NODE, str(BRIDGE), "login-qr", "--account", account_id, "--creds-dir", str(ACCOUNTS_DIR), "--emit", str(emit)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    _qr_procs[account_id] = p
    db.upsert_account(account_id, status="pending_qr", creds_path=str(account_creds_path(account_id)))
    db.log_event("qr_start", account_id=account_id)
    return emit


def qr_status(account_id: str) -> dict:
    emit = ACCOUNTS_DIR / f"{account_id}.qr.log"
    events = []
    if emit.exists():
        for line in emit.read_text().splitlines()[-20:]:
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    last = events[-1] if events else None
    if last and last.get("event") == "qr" and last.get("image"):
        img = last["image"]
        if not str(img).startswith("data:"):
            last["image"] = "data:image/png;base64," + img
    return {"account_id": account_id, "events": events, "last": last,
            "logged_in": is_logged_in(account_id)}


# -------------------------------------------------------------- account info

def _digits(s: str) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def is_real_phone(s: str) -> bool:
    """True for a Vietnammese phone number (0xxxxxxxxx / 84xxxxxxxxx), False
    for long Zalo user ids (19-20 digits) that occupy the 'phone' column."""
    d = _digits(s)
    if not d:
        return False
    if d.startswith("84") and 11 <= len(d) <= 12:
        return True
    if d.startswith("0") and 9 <= len(d) <= 11:
        return True
    return False


def norm_phone(s: str) -> str:
    """Canonicalise a phone number so the SAME person maps to ONE key.

    '0912345678', '+84912345678' and '84912345678' -> '0912345678'.
    Delegates to db.canon_phone so the engine and the store always agree.
    """
    return db.canon_phone(s)


def _norm_identity(phone: str, zalo_uid: str | None) -> str:
    """Best identity for a recipient: real Zalo user id wins over phone.

    Used so the same human is not targeted twice across campaigns, even when
    the phone is spelled differently (F4) or only one list carries the userId.
    """
    return db.canon_ident(phone, zalo_uid)


def _is_auth_error(err: str) -> bool:
    low = (err or "").lower()
    return any(k in low for k in (
        "not logged", "not login", "chưa đăng nhập", "đăng nhập", "login",
        "session", "unauthor", "expired"))


def alias_for(name: str, phone: str) -> str | None:
    """Desired Zalo alias (bieät danh) for a contact: name + phone when we have
    a real phone number; None (keep the current name) when there is no phone."""
    name = (name or "").strip()
    phone = (phone or "").strip()
    if not is_real_phone(phone):
        return None
    return f"{name} - {phone}" if name else phone


def set_friend_alias(account_id: str, user_id: str, alias: str) -> dict:
    """Set the Zalo friend-alias (nickname/biet danh) for a user id. No-op for
    an empty alias (keeps the current name)."""
    alias = (alias or "").strip()
    user_id = (user_id or "").strip()
    if not user_id or not alias:
        return {"ok": False, "error": "thiếu user id hoặc tên"}
    if not is_logged_in(account_id):
        return {"ok": False, "error": "not logged in"}
    return _safe_cmd(account_id, {"cmd": "set-alias", "userId": user_id, "alias": alias})


def refresh_account_info(account_id: str) -> dict:
    if not is_logged_in(account_id):
        return {"ok": False, "error": "not logged in"}
    r = _safe_cmd(account_id, {"cmd": "whoami"})
    prof = (r.get("profile") or {}) if r.get("ok") else {}
    if r.get("ok"):
        db.upsert_account(account_id,
                          user_id=str(prof.get("userId") or ""),
                          display_name=prof.get("displayName") or prof.get("zaloName") or "",
                          status="active", last_seen=int(time.time()))
    else:
        db.upsert_account(account_id, status="error")
    return r


# ------------------------------------------------------ account management

def account_stats(account_id: str) -> dict:
    """Today's usage vs caps + last activity for one account (read-only)."""
    d = settings().get("defaults", {})
    now = int(time.time())
    sent_hour = db.count_sent_since(account_id, now - 3600)
    sent_day = db.count_sent_since(account_id, now - 86400)
    friend_day = db.count_sent_since(account_id, now - 86400, kind="friend")
    scans_hour = db.scan_counts(account_id, now - 3600)
    scans_day = db.scan_counts(account_id, now - 86400)
    return {
        "sent_hour": sent_hour, "sent_day": sent_day, "friend_day": friend_day,
        "scans_hour": scans_hour, "scans_day": scans_day,
        "max_per_hour": int(d.get("max_per_hour", 30)),
        "max_per_day": int(d.get("max_per_day", 200)),
        "max_friend_per_day": int(d.get("max_friend_per_day", 20)),
        "group_scan_per_hour": int(d.get("group_scan_per_hour", 20)),
        "group_scan_per_day": int(d.get("group_scan_per_day", 60)),
        "last_sent": db.last_sent_ts(account_id),
        "last_scan": db.last_scan_ts(account_id),
    }


def _fmt_ts(ts: int) -> str:
    if not ts:
        return "—"
    return time.strftime("%H:%M %d/%m", time.localtime(ts))


def account_facts(account_id: str) -> dict:
    """One ready-to-render bundle for the Accounts tab."""
    a = db.get_account(account_id) or {"id": account_id}
    st = account_stats(account_id)
    st["last_sent_fmt"] = _fmt_ts(st["last_sent"])
    st["last_scan_fmt"] = _fmt_ts(st["last_scan"])
    return {
        "stats": st,
        "logged_in": is_logged_in(account_id),
        "enabled": bool(a.get("enabled", 1)),
        "status": a.get("status") or "",
        "creds_path": str(account_creds_path(account_id)),
        "creds_exists": is_logged_in(account_id),
        "stale": (bool(a.get("user_id")) and not is_logged_in(account_id)),
    }


def account_session_ok(account_id: str) -> dict:
    """Probe a live 'whoami' to check whether the stored session still works.
    On auth failure, flip the stored status to 'expired' so the UI can warn."""
    if not is_logged_in(account_id):
        return {"ok": False, "error": "chưa có credentials — cần đăng nhập"}
    r = _safe_cmd(account_id, {"cmd": "whoami"}, timeout=30)
    if r.get("ok"):
        prof = r.get("profile") or {}
        db.upsert_account(account_id,
                          user_id=str(prof.get("userId") or ""),
                          display_name=prof.get("displayName") or prof.get("zaloName") or "",
                          status="active", last_seen=int(time.time()))
        return {"ok": True, "status": "active", "name": prof.get("displayName") or prof.get("zaloName") or ""}
    db.upsert_account(account_id, status="expired")
    return {"ok": False, "status": "expired", "error": str(r.get("error") or "session hết hạn")[:200]}


def set_account_enabled(account_id: str, enabled: bool) -> dict:
    """Turn an account on/off. A disabled account is never used for live sends
    or group scans (kept warm instead of deleted)."""
    if not db.get_account(account_id):
        return {"ok": False, "error": "tài khoản không tồn tại"}
    db.upsert_account(account_id, enabled=1 if enabled else 0)
    db.log_event("account_enable" if enabled else "account_disable", account_id=account_id)
    log(f"ACCOUNT {'enabled' if enabled else 'disabled'} :: {account_id}")
    return {"ok": True, "enabled": bool(enabled)}


def rename_account(account_id: str, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "tên trống"}
    if not db.get_account(account_id):
        return {"ok": False, "error": "tài khoản không tồn tại"}
    db.upsert_account(account_id, name=name)
    db.log_event("account_rename", f"{account_id} -> {name}", account_id=account_id)
    return {"ok": True, "name": name}


def delete_account(account_id: str, remove_creds: bool = True) -> dict:
    """Delete an account row and (optionally) its credential + QR files."""
    a = db.get_account(account_id)
    if not a:
        return {"ok": False, "error": "tài khoản không tồn tại"}
    # stop any pending QR process
    p = _qr_procs.pop(account_id, None)
    if p and p.poll() is None:
        p.terminate()
    db.delete_account(account_id)
    removed = []
    if remove_creds:
        for f in (account_creds_path(account_id), ACCOUNTS_DIR / f"{account_id}.qr.log"):
            try:
                if f.exists():
                    f.unlink()
                    removed.append(str(f))
            except OSError:
                pass
    db.log_event("account_delete", f"{account_id} creds={remove_creds}")
    log(f"ACCOUNT deleted :: {account_id} (creds={remove_creds})")
    return {"ok": True, "deleted": account_id, "creds_removed": removed}


# ------------------------------------------------------------ message templating

DEFAULT_GENDER_MAP = {"male": "Anh", "female": "Chị", "unknown": "Anh/Chị"}


def normalize_text(text: str) -> str:
    """Normalize line endings for chat messages.

    Browser <textarea> posts CRLF (\r\n). Zalo treats \r AND \n as separate
    line breaks, so CRLF shows up as a BLANK LINE between every line (lines
    look double-spaced vs. the original). Collapse every line ending to a
    single \n and trim trailing spaces per line.
    """
    if not text:
        return ""
    t = str(text).replace("\r\n", "\n").replace("\r", "\n")
    # Also drop any leftover Unicode line/paragraph separators + trailing
    # whitespace on each line (invisible chars users can't see but Zalo renders).
    t = t.replace("\u2028", "\n").replace("\u2029", "\n").replace("\u0085", "\n")
    lines = [ln.rstrip() for ln in t.split("\n")]
    return "\n".join(lines).strip("\n")


def render_message(text: str, *, name: str = "", gender: str = "") -> str:
    """Personalize a message body.

    Supported placeholders:
      {name} / {ten} / {full_name}  -> recipient display name
      {salutation} / {anh_chi}     -> gender-aware honorific (Anh / Chị)
      {gioi_tinh}                  -> 'nam' / 'nữ' / ''
    Unknown gender falls back to defaults.gender_map.unknown.
    """
    if not text:
        return ""
    gmap = {**DEFAULT_GENDER_MAP, **(settings().get("defaults", {}).get("gender_map") or {})}
    g = (gender or "").strip().lower()
    if g in ("m", "nam", "male", "1"):
        key = "male"
    elif g in ("f", "nu", "nữ", "female", "2"):
        key = "female"
    else:
        key = "unknown"
    sal = gmap.get(key, gmap["unknown"])
    disp = name or gmap["unknown"]
    repl = {
        "name": disp,
        "ten": disp,
        "full_name": disp,
        "salutation": sal,
        "anh_chi": sal,
        "Anh_Chị": sal,
        "gioi_tinh": {"male": "nam", "female": "nữ"}.get(key, ""),
    }
    out = text
    for k, v in repl.items():
        out = out.replace("{" + k + "}", str(v))
    return normalize_text(out)


# ------------------------------------------------------- rate limit / engine

_JOBS: "queue.Queue[dict]" = queue.Queue()
_worker_started = False
_worker_lock = threading.RLock()  # reentrant: _ensure_worker -> _ensure_scheduler
_send_lock = threading.Lock()  # serialize real sends (gap/caps global)
_queued_targets: set[int] = set()  # target ids currently in the send queue (double-click guard)
_queued_lock = threading.Lock()    # guards _queued_targets / _busy (atomic claim / reconcile)
_busy: set[int] = set()            # target ids being processed right now
_next_attempt: dict[int, float] = {}  # target_id -> earliest ts to attempt next (backoff)
_delayed: list[tuple[float, dict]] = []  # (due_ts, job) waiting for backoff
_delayed_lock = threading.Lock()
_sched_started = False
_campaign_paused: set[str] = set()
_paused_jobs: dict[str, list[dict]] = {}  # cid -> jobs parked while paused
_fail_streak: dict[str, int] = {}  # account -> consecutive transient failures

# Account-level throttle detected at the send stage (e.g. Zalo refusing a thread
# type / rate limit such as "Tham số không hợp lệ" or "...thử lại vào HH:MM").
# While such a signal is fresh, don't spawn a bridge send per target (that would
# hot-loop hundreds of targets); park jobs until the cooldown passes so the
# panel (and the Zalo account) recover instead of retrying forever.
_acct_send_block: dict[str, float] = {}  # account -> epoch ts until which sends are parked
_ACCT_BLOCK_S = 900            # default cooldown when no explicit reset time parsed
_ACCT_BLOCK_MAX_S = 3 * 3600   # safety ceiling for a parsed reset time

def _parse_retry_at(err: str, now: float) -> float | None:
    """If a Zalo error names a reset clock (\"...thử lại vào 13:00\"), return the
    epoch ts for that time (today, or tomorrow when already past) so a caller can
    honour the server-requested cooldown instead of guessing."""
    m = re.search(r"(\d{1,2}):(\d{2})", str(err or ""))
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    lt = time.localtime(now)
    cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if cand <= now:
        cand += 86400
    return cand

def _acct_send_blocked(acct: str) -> float:
    """Seconds left on the account send cooldown (0 = not blocked)."""
    until = _acct_send_block.get(acct, 0)
    return max(0.0, until - time.time())

def _note_acct_send_block(acct: str, remaining: float) -> None:
    _acct_send_block[acct] = time.time() + max(30.0, min(float(remaining), _ACCT_BLOCK_MAX_S))


def _cap_ok(account_id: str, defaults: dict) -> tuple[bool, str]:
    now = int(time.time())
    hour = db.count_sent_since(account_id, now - 3600)
    day = db.count_sent_since(account_id, now - 86400)
    if hour >= int(defaults.get("max_per_hour", 30)):
        return False, f"hour cap {hour}/{defaults.get('max_per_hour')}"
    if day >= int(defaults.get("max_per_day", 200)):
        return False, f"day cap {day}/{defaults.get('max_per_day')}"
    return True, ""


def _friend_cap_ok(account_id: str, defaults: dict) -> tuple[bool, str]:
    now = int(time.time())
    day = db.count_sent_since(account_id, now - 86400, kind="friend")
    if day >= int(defaults.get("max_friend_per_day", 20)):
        return False, f"friend day cap {day}/{defaults.get('max_friend_per_day')}"
    return True, ""


def enqueue(job: dict, claim: bool = True) -> None:
    """Queue a send job.

    claim=True (default): take the target claim first and skip if it is already
    claimed (idempotent — protects ad-hoc callers). claim=False: the caller has
    ALREADY claimed the target via _claim_target (start_campaign) so just queue.
    """
    tid = job.get("target_id")
    if claim:
        if not _claim_target(tid):
            _ensure_worker()
            return
    _JOBS.put(job)
    _ensure_worker()


def _claim_target(tid) -> bool:
    """Atomically claim a target before the caller enqueues it for it.

    Returns True when the claim was newly taken, False when it was already
    pending (another start call / a still-queued job owns it). This closes the
    TOCTOU window in start_campaign where two concurrent 'Chạy' clicks could
    each enqueue the same target (F1: double-send).
    """
    if tid is None:
        return True
    with _queued_lock:
        if tid in _queued_targets:
            return False
        _queued_targets.add(tid)
        return True


def _release_target(tid) -> None:
    with _queued_lock:
        _queued_targets.discard(tid)
        _next_attempt.pop(tid, None)


def _ensure_scheduler() -> None:
    """Start the delayed-retry pump. It moves jobs whose backoff window has
    elapsed back onto the work queue so the SINGLE worker never has to sleep
    (F5: one throttled campaign must not stall the others)."""
    global _sched_started
    with _worker_lock:
        if _sched_started:
            return
        _sched_started = True
        threading.Thread(target=_scheduler_loop, name="zs-sched", daemon=True).start()


def _scheduler_loop() -> None:
    while True:
        time.sleep(0.5)
        now = time.time()
        due: list[dict] = []
        with _delayed_lock:
            keep: list[tuple[float, dict]] = []
            for ts, j in _delayed:
                (due if ts <= now else keep).append((ts, j))
            _delayed[:] = keep
        for _, j in due:
            _JOBS.put(j)


def _reconcile_queue() -> None:
    """Rebuild _queued_targets from the jobs actually in the queue (F6).

    A target can end up stranded as 'pending' (start_campaign skipped it
    because it was in _queued_targets, then that job was dropped) — leaving it
    pending forever with nothing queued. Reconcile keeps the set equal to the
    truth: ids still queued + the job currently being processed.
    """
    live = set(_busy)
    with _JOBS.mutex:
        live |= {j.get("target_id") for j in list(_JOBS.queue)}
    for jobs in _paused_jobs.values():
        live |= {j.get("target_id") for j in jobs}
    with _delayed_lock:
        live |= {j.get("target_id") for _, j in _delayed}
    with _queued_lock:
        _queued_targets.clear()
        _queued_targets.update(t for t in live if t is not None)


def queued_count() -> int:
    with _queued_lock:
        return len(_queued_targets)


def queue_depth() -> int:
    return _JOBS.qsize()


def _drain_campaign_jobs(cid: str, release: bool = True) -> list[dict]:
    """Remove every queued job for cid from the FIFO; return them (others stay).

    release=True (default) frees each pulled target from the claim set. Callers
    that immediately re-queue the jobs (pause/bake) pass release=False so the
    claim is preserved and can't be stolen mid-flight.
    """
    out: list[dict] = []
    keep: list[dict] = []
    while not _JOBS.empty():
        try:
            j = _JOBS.get_nowait()
        except Exception:
            break
        (out if j.get("campaign_id") == cid else keep).append(j)
        _JOBS.task_done()
    for j in keep:
        _JOBS.put(j)
    # any target whose job we just pulled must be released from the claim set so
    # a later start/retry can re-enqueue it (unless the caller re-queues them)
    if release:
        for j in out:
            _release_target(j.get("target_id"))
    return out


def pause_campaign(cid: str, paused: bool) -> None:
    if paused:
        _campaign_paused.add(cid)
        parked = _paused_jobs.setdefault(cid, [])
        parked.extend(_drain_campaign_jobs(cid, release=False))
        db.set_campaign_status(cid, "paused", finished_at=int(time.time()))
        log(f"campaign paused, parked {len(parked)} jobs :: {cid}")
    else:
        _campaign_paused.discard(cid)
        db.set_campaign_status(cid, "running")
        for j in _paused_jobs.pop(cid, []):
            _JOBS.put(j)
        _ensure_worker()


def resume_campaign(cid: str) -> dict:
    """Clear pause, re-enqueue parked jobs, and let the worker continue."""
    _campaign_paused.discard(cid)
    db.set_campaign_status(cid, "running")
    parked = _paused_jobs.pop(cid, [])
    for j in parked:
        _JOBS.put(j)
    db.log_event("campaign_resume", cid, campaign_id=cid)
    log(f"campaign resumed, requeued {len(parked)} jobs :: {cid}")
    _ensure_worker()
    return {"ok": True, "status": "running", "requeued": len(parked)}


def stop_campaign(cid: str) -> dict:
    """Hard-stop: drop all queued/parked jobs and mark remaining targets skipped."""
    _campaign_paused.discard(cid)
    parked = _paused_jobs.pop(cid, [])
    dropped = parked + _drain_campaign_jobs(cid)
    for j in dropped:
        _release_target(j.get("target_id"))
    n = db.skip_pending_targets(cid, reason="stopped")
    db.set_campaign_status(cid, "done", finished_at=int(time.time()))
    db.log_event("campaign_stop", f"{cid}: bỏ {n} SĐT còn lại", campaign_id=cid)
    log(f"CAMPAIGN stopped :: {cid} (dropped {len(dropped)} jobs, skipped {n})")
    return {"ok": True, "stopped": cid, "skipped": n}


def _bake_dry(cid: str, dry: bool) -> int:
    """Re-stamp every queued + parked job of cid with a new dry_run flag so a
    DRY<->LIVE toggle takes effect for already-scheduled targets (otherwise an
    in-flight job keeps sending with the flag it was born with)."""
    val = bool(dry)
    jobs = _drain_campaign_jobs(cid, release=False)
    parked = _paused_jobs.get(cid, [])
    for j in jobs:
        j["dry_run"] = val
    for j in parked:
        j["dry_run"] = val
    for j in jobs:
        _JOBS.put(j)
    return len(jobs) + len(parked)


def set_campaign_dry(cid: str, dry: bool) -> dict:
    """Toggle DRY/LIVE and re-bake the flag into all not-yet-sent jobs."""
    camp = db.get_campaign(cid)
    if not camp:
        return {"ok": False, "error": "campaign not found"}
    db.set_campaign_dry(cid, 1 if dry else 0)
    baked = _bake_dry(cid, dry)
    db.log_event("campaign_dry", f"{cid} dry={bool(dry)} rebaked={baked}", campaign_id=cid)
    log(f"CAMPAIGN dry={bool(dry)} :: {cid} (re-baked {baked} jobs)")
    return {"ok": True, "dry_run": bool(dry), "rebaked": baked}


def retry_failed(cid: str, statuses: tuple = ("failed", "skipped")) -> dict:
    """Reset parked (failed/skipped) targets back to pending so the operator can
    re-run exactly the ones that didn't go through."""
    if not db.get_campaign(cid):
        return {"ok": False, "error": "campaign not found"}
    n = db.reset_targets(cid, list(statuses))
    db.log_event("campaign_retry", f"{cid} reset {n} {','.join(statuses)}->pending", campaign_id=cid)
    log(f"CAMPAIGN retry :: {cid} reset {n}")
    return {"ok": True, "reset": n}


def delete_campaign(cid: str) -> dict:
    """Delete a campaign and stop any of its queued/parked jobs."""
    _campaign_paused.discard(cid)
    _paused_jobs.pop(cid, None)
    tids = {t["id"] for t in db.targets_for(cid, 100000)}
    _drain_campaign_jobs(cid)
    for tid in tids:
        _release_target(tid)
    db.delete_campaign(cid)
    db.log_event("campaign_delete", cid)
    log(f"CAMPAIGN deleted :: {cid}")
    return {"ok": True, "deleted": cid}


def _ensure_worker() -> None:
    global _worker_started
    # Campaign sending is owned solely by the campaign role. In a G2G-only
    # process there is no sender, so campaign actions there can never emit Zalo
    # traffic (isolation guarantee).
    if not role_allows_campaign():
        return
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        threading.Thread(target=_worker_loop, name="zs-worker", daemon=True).start()
        _ensure_scheduler()


def _worker_loop() -> None:
    log("worker started")
    while True:
        job = _JOBS.get()
        tid = job.get("target_id")
        # Hold the busy flag for the WHOLE job, starting here — before the first
        # line of _process_job. The worker pops the job off _JOBS before calling
        # _process_job, so without this there is a window where the target is
        # neither queued nor busy; a concurrent _reconcile_queue (F6) then sees
        # its claim as stale, drops it, and a second 'Chạy' can re-enqueue the
        # same target (double-send, F1).
        if tid is not None:
            _busy.add(tid)
        try:
            _process_job(job)
        except Exception as exc:  # never die
            log(f"job error: {exc!r}")
        finally:
            if tid is not None:
                _busy.discard(tid)
            _JOBS.task_done()


def _process_job(job: dict) -> None:
    defaults = settings().get("defaults", {})
    cid = job.get("campaign_id")
    acct = job.get("account_id") or "default"
    tid = job.get("target_id")

    # Hard stop (killswitch): requeue and wait, but do NOT hot-spin the single
    # worker (that would starve the queue). Back off and re-check.
    if killswitch_active():
        db.mark_target(tid, "pending", error="killswitch")
        if tid is not None:
            _next_attempt[tid] = time.time() + 10
        _ensure_scheduler()
        with _delayed_lock:
            _delayed.append((time.time() + 10, job))
        return

    # Paused campaign: park the job (no requeue -> no hot-spin). It is
    # re-enqueued by resume/start.
    if cid and cid in _campaign_paused:
        db.mark_target(tid, "pending", error="paused")
        _paused_jobs.setdefault(cid, []).append(job)
        return

    # Campaign was deleted mid-flight: drop the job silently
    if cid and not db.get_campaign(cid):
        _release_target(tid)
        return

    # Backoff window from a previous transient failure: requeue (no sleep so the
    # single worker keeps draining other campaigns) and let a later pass retry.
    wait = (float(_next_attempt.get(tid, 0)) or 0) - time.time()
    if wait > 0:
        _JOBS.put(job)
        time.sleep(min(wait, 5.0))
        return

    # Serialize sends so gap/caps can't be beaten by concurrent enqueues. The
    # blocking sleeps now live OUTSIDE this lock (see _do_send_job), so a
    # rate-limited campaign can no longer freeze every other campaign (F5).
    # _busy is held for the ENTIRE job by _worker_loop, so (unlike before) it is
    # not toggled here: reconcile must still see the target as in-flight on the
    # early park/drop returns above, or a second start could re-enqueue it (F1).
    with _send_lock:
        _do_send_job(job, defaults, cid, acct)


def _requeue_wait(job: dict, error: str, delay: float) -> None:
    """Park a job for a later attempt WITHOUT sleeping the worker thread.

    The delay is recorded in _next_attempt and the job handed to the scheduler
    (which re-queues it when due), so a throttled target never blocks the queue.
    task_done() is already handled by _process_job.
    """
    tid = job.get("target_id")
    db.mark_target(tid, "pending", error=error)
    if tid is not None:
        _next_attempt[tid] = time.time() + max(0.0, float(delay))
    _ensure_scheduler()
    with _delayed_lock:
        _delayed.append((time.time() + max(0.0, float(delay)), job))


def send_bridge_args(job: dict, acct: str) -> list[str]:
    """Build the bridge argv for one send job (message or friend-request).

    For message jobs, a non-empty `blocks` list is passed as a single JSON arg
    so the bridge sends text/image/link blocks in order; otherwise the legacy
    --url/--images/--file-caption/--text flags are used.
    """
    base = ["--account", acct, "--creds-dir", str(ACCOUNTS_DIR), "--json"]
    if job.get("kind") == "friend":
        args = ["friend-request", "--user", job["thread"], *base]
        if job.get("text"):
            args += ["--message", job["text"]]
        return args
    args = ["send", "--thread", job["thread"], "--type", job.get("type", "user"), *base]
    blocks = job.get("blocks")
    if blocks:
        # Ordered block list (text/image/link) sent in sequence by the bridge.
        args += ["--blocks", json.dumps(blocks, ensure_ascii=False)]
        return args
    if job.get("url"):
        args += ["--url", job["url"]]
    imgs = [p for p in (job.get("images") or []) if p and Path(p).exists()]
    if imgs:
        # multi-photo album; the template text becomes the album caption
        args += ["--images", ",".join(imgs)]
        if job.get("file_caption"):
            args += ["--file-caption", job["file_caption"]]
    if job.get("text"):
        args += ["--text", job["text"]]
    return args


def _do_send_job(job: dict, defaults: dict, cid: str, acct: str) -> None:
    # (killswitch / pause re-checked under the lock as well)
    if killswitch_active():
        _requeue_wait(job, "killswitch", 10)
        return

    # DRY-RUN: simulate only. No Zalo traffic -> skip caps/gap/login so test
    # runs are instant and never trip the anti-ban throttle.
    if bool(job.get("dry_run", True)):
        db.mark_target(job["target_id"], "sent", msg_id="dry-run")
        n_img = len(job.get("images") or [])
        db.log_event("send_dry", f"{job.get('thread')} :: {(job.get('text') or '')[:60]}"
                     + (f" :: {n_img} ảnh" if n_img else ""),
                     account_id=acct, campaign_id=cid)
        log(f"DRY send -> {job.get('thread')}" + (f" (+{n_img} ảnh)" if n_img else ""))
        _release_target(job["target_id"])
        _maybe_finish(cid)
        return

    # Quiet hours: no LIVE message/friend sends during the operator's window.
    # Park (no sleep, no cap burn) and re-check soon, so the campaign resumes
    # automatically when the window closes.
    if in_quiet_hours(defaults):
        _requeue_wait(job, "giờ im lặng", min(quiet_seconds_left(defaults), 600) or 60)
        return

    # Rate caps
    ok, why = _cap_ok(acct, defaults)
    if job.get("kind") == "friend":
        ok2, why2 = _friend_cap_ok(acct, defaults)
        ok, why = (ok and ok2), (why or why2)
    if not ok:
        log(f"rate-limited ({why}); requeue in 30s")
        _requeue_wait(job, f"rate:{why}", 30)
        return

    # Don't spawn a bridge call for a nick that isn't logged in — park the job
    # (no sleep) so a later QR login can resume without burning the campaign,
    # and other campaigns keep flowing.
    if not is_logged_in(acct):
        _requeue_wait(job, "chưa đăng nhập", 60)
        return

    # Account disabled by the operator: keep the job parked (zero live traffic)
    # until the nick is switched back on — do not burn the campaign.
    if not account_enabled(acct):
        _requeue_wait(job, "nick đang tắt", 60)
        return

    # Min-gap + jitter. Computed OUTSIDE _send_lock; if we still owe a gap the
    # job is parked (no blocking sleep) so the single worker stays responsive
    # for other campaigns (F5).
    gap = int(defaults.get("min_gap_seconds", 25)) + random.randint(0, int(defaults.get("jitter_seconds", 15)))
    last = db.last_sent_ts(acct)
    wait = (last + gap) - time.time()
    if last and wait > 0:
        log(f"throttle {wait:.0f}s (gap {gap}s)")
        _requeue_wait(job, f"gap:{int(wait)}s", min(wait, 60))
        return

    # Account send cooldown: a previous transient Zalo error (e.g. "Tham số
    # không hợp lệ" / "thử lại vào HH:MM") parks ALL of this account's sends so
    # we don't spawn hundreds of doomed bridge calls in a hot loop.
    left = _acct_send_blocked(acct)
    if left > 0:
        _requeue_wait(job, f"acct_block:{int(left)}s", min(left, 300))
        return

    args = send_bridge_args(job, acct)
    # Serialization is owned by the caller: _process_job holds _send_lock for the
    # WHOLE _do_send_job call. Do NOT re-acquire it here — _send_lock is a plain
    # (non-reentrant) Lock, so a nested acquire deadlocks this thread forever
    # (campaign stuck 'running', targets frozen 'pending', no bridge spawned).
    # Route the send through the SHARED per-account session daemon (one Zalo
    # session for the whole app) instead of spawning a fresh `zalo.mjs send`
    # login, which would kick the G2G listener off the same account.
    if job.get("kind") == "friend":
        sreq = {"cmd": "friend-request", "userId": job.get("thread"), "message": job.get("text")}
    else:
        imgs = [p for p in (job.get("images") or []) if p and Path(p).exists()]
        sreq = {"cmd": "send", "thread": job.get("thread"), "type": job.get("type", "user"),
                "text": job.get("text"), "url": job.get("url"), "images": imgs,
                "blocks": job.get("blocks"), "fileText": job.get("file_caption")}
    r = sessiond_client.cmd(acct, sreq, timeout=180)
    if r.get("ok"):
        note = str(r.get("msgId") or "")
        n_img = int(r.get("images") or 0)
        if n_img:
            note = f"{note} (+{n_img} ảnh)"
        # Feature: if the contact has a saved name, set it as the Zalo friend
        # alias ("biệt danh") so the chat list shows the customer name; keep the
        # current name when we have none.
        if defaults.get("set_alias_on_send"):
            a_uid = job.get("alias_uid") or job.get("thread")
            phone = job.get("alias_phone") or ""
            if not is_real_phone(phone):
                phone = job.get("thread") if is_real_phone(job.get("thread")) else ""
            desired = alias_for(job.get("alias_name") or "", phone)
            if a_uid and desired:
                try:
                    ar = set_friend_alias(acct, a_uid, desired)
                    if ar.get("ok"):
                        db.log_event("alias_set", f"{a_uid} -> {desired}",
                                     account_id=acct, campaign_id=cid)
                    else:
                        log(f"alias set failed ({desired!r}): {ar.get('error')}")
                except Exception as e:
                    log(f"alias set error: {e}")
        db.mark_target(job["target_id"], "sent", msg_id=note)
        db.log_event("send_ok", f"{job.get('thread')}" + (f" :: {n_img} ảnh" if n_img else ""),
                     account_id=acct, campaign_id=cid)
        log(f"SENT -> {job.get('thread')}" + (f" (+{n_img} ảnh)" if n_img else ""))
        _fail_streak.pop(acct, None)
    else:
        err = str(r.get("error") or "")
        low = err.lower()
        # Permanent, per-recipient conditions: never retry these (they would
        # loop forever) — mark failed with a clear reason.
        if any(k in low for k in ("chặn", "block", "từ chối nhận tin", "người lạ",
                                  "not a friend", "không phải bạn", "không tồn tại",
                                  "not found", "ngừng hoạt động", "deleted", "blocked")):
            db.mark_target(job["target_id"], "failed", error=err[:200])
            db.log_event("send_fail", f"{job.get('thread')} :: {err[:200]}",
                         account_id=acct, campaign_id=cid)
            log(f"FAIL(perm) -> {job.get('thread')} :: {err[:120]}")
            _release_target(job["target_id"])
            _maybe_finish(cid)
            return
        # Transient problems must NOT burn the whole campaign: requeue with
        # backoff so a re-login, a pause-scan window, or a network blip can
        # recover. NOTE: Node/undici surfaces a dropped socket as a generic
        # "fetch failed" with NO status code — that is a transport error, not a
        # per-recipient rejection, so it must be here (a network outage
        # otherwise fails every in-flight target permanently).
        NET_KEYS = ("econnreset", "econnrefused", "enotfound", "eai_again", "etimedout",
                    "epipe", "socket hang up", "fetch failed", "network", "getaddrinfo",
                    "connection reset", "connection refused", "dns", "ehostunreach",
                    "enetunreach", "network is unreachable", "timed out")
        TRANS_KEYS = ("not logged", "not login", "chưa đăng nhập", "đăng nhập", "login",
                      "session", "timeout", "unauthor", "expired", "tham số không hợp lệ",
                      "invalid parameter", "rate", "too many")
        if any(k in low for k in (*NET_KEYS, *TRANS_KEYS)):
            db.mark_target(job["target_id"], "pending", error=f"retry:{err[:120]}")
            n = _fail_streak.get(acct, 0) + 1
            _fail_streak[acct] = n
            if any(k in low for k in NET_KEYS):
                # Network blip: retry fast (one gap-slot, not a 15-min freeze) and
                # do NOT trip the long account cooldown — the link is usually
                # back within seconds, and we must not stall recovery ~15 min.
                backoff = min(3 * n, 30)
                log(f"RETRY(net) ({err[:70]}) #{n}; requeue in {backoff}s")
            else:
                # Auth / thread-type / rate limit can hot-loop the whole campaign
                # -> park ALL of this account's sends for a cooldown.
                reset = _parse_retry_at(err, time.time())
                _note_acct_send_block(acct, (reset - time.time()) if reset else _ACCT_BLOCK_S)
                backoff = min(int(defaults.get("min_gap_seconds", 900)) * n, 1800)
                log(f"RETRY ({err[:80]}) #{n}; requeue in {backoff}s")
            if n == 5:
                db.log_event("send_warn", f"{acct}: 5 lỗi liên tiếp ({err[:80]}) — kiểm tra đăng nhập",
                             account_id=acct, campaign_id=cid)
            _requeue_wait(job, f"retry:{err[:80]}", backoff)
            return
        db.mark_target(job["target_id"], "failed", error=err[:300])
        db.log_event("send_fail", f"{job.get('thread')} :: {err}", account_id=acct, campaign_id=cid)
        log(f"FAIL -> {job.get('thread')} :: {err}")
    _release_target(job["target_id"])
    _maybe_finish(cid)


# ---------------------------------------------------------- message blocks
#
# A campaign body is a SEQUENCE of ordered blocks instead of "one text + one
# album", so an operator can interleave text and photos (and caption a photo):
#   [{"type":"text",  "content":"Xin chào {salutation} {name}"},
#    {"type":"image", "src":"/abs/path.jpg", "caption":"ưu đãi"},
#    {"type":"text",  "content":"Chi tiết: https://..."}]
#
# Backward compatibility: legacy campaigns (body_template + images[]) are parsed
# into the same block list on read, so nothing needs migrating: the text block
# comes first, then the album images (ordered), then the optional link block.

_BLOCK_TYPES = ("text", "image", "link")


def _norm_block(b: object) -> dict | None:
    """Coerce one raw block into a safe canonical block, or None to drop it."""
    if not isinstance(b, dict):
        return None
    t = str(b.get("type") or "").strip().lower()
    if t not in _BLOCK_TYPES:
        return None
    if t == "text":
        content = normalize_text(b.get("content") or "")
        return {"type": "text", "content": content} if content else None
    if t == "image":
        src = str(b.get("src") or b.get("path") or "").strip()
        if not src:
            return None
        return {"type": "image", "src": src,
                "caption": normalize_text(b.get("caption") or "")}
    url = str(b.get("url") or "").strip()
    if not url:
        return None
    return {"type": "link", "url": url, "caption": normalize_text(b.get("caption") or "")}


def parse_block_list(raw) -> list[dict]:
    """Parse a raw JSON-list (string or list) into clean blocks; [] on junk."""
    if raw is None or raw == "":
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else list(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [b for b in (_norm_block(x) for x in data) if b]


def parse_blocks(camp: dict | None) -> list[dict]:
    """Return the ordered block list for a campaign.

    Prefers the stored `blocks` JSON; falls back to legacy body/images/url so
    old campaigns keep working without a migration.
    """
    camp = camp or {}
    raw = camp.get("blocks")
    blocks = parse_block_list(raw) if raw else []
    if blocks:
        return blocks
    # Stored blocks empty/unusable (e.g. legacy campaign after the column was
    # added) -> rebuild from body_template + album images + url.
    # Legacy shape -> text block, then album images, then a link block.
    blocks: list[dict] = []
    body = normalize_text(camp.get("body_template") or "")
    if body:
        blocks.append({"type": "text", "content": body})
    for p in campaign_images(camp):
        blocks.append({"type": "image", "src": p, "caption": ""})
    url = str(camp.get("url") or "").strip()
    if url:
        blocks.append({"type": "link", "url": url, "caption": ""})
    return blocks


def blocks_to_json(blocks: list[dict]) -> str:
    """Serialize a block list, dropping anything malformed."""
    clean = [b for b in (_norm_block(x) for x in (blocks or [])) if b]
    return json.dumps(clean, ensure_ascii=False)


def compile_blocks(blocks: list[dict], *, name: str = "", gender: str = "") -> list[dict]:
    """Personalize text/link-caption blocks for one recipient and drop empties.

    Image blocks keep their src; their caption is personalized too. The result
    is what the bridge receives (JSON array) for a single recipient.
    """
    res: list[dict] = []
    for b in (blocks or []):
        nb = _norm_block(b)
        if not nb:
            continue
        t = nb.get("type")
        if t == "text":
            content = render_message(nb.get("content") or "", name=name, gender=gender)
            if content:
                res.append({"type": "text", "content": content})
        elif t == "image":
            if nb.get("src") and Path(nb["src"]).exists():
                res.append({"type": "image", "src": nb["src"],
                            "caption": render_message(nb.get("caption") or "", name=name, gender=gender)})
        elif t == "link":
            res.append({"type": "link", "url": nb.get("url"),
                        "caption": render_message(nb.get("caption") or "", name=name, gender=gender)})
    return res


def blocks_summary(blocks: list[dict]) -> str:
    """Short human summary like 'text(42) · 3 ảnh · link' for logs/lists."""
    ntext = sum(1 for b in blocks if b.get("type") == "text")
    nimg = sum(1 for b in blocks if b.get("type") == "image")
    nlink = sum(1 for b in blocks if b.get("type") == "link")
    parts = []
    if ntext:
        parts.append(f"{ntext} text")
    if nimg:
        parts.append(f"{nimg} ảnh")
    if nlink:
        parts.append(f"{nlink} link")
    return " · ".join(parts) or "(trống)"


def _img_batches(blocks: list[dict]) -> list[dict]:
    """Collapse consecutive image blocks into album payloads so the bridge sends
    one album per run of photos, interleaved in order with the text blocks."""
    out: list[dict] = []
    run: list[dict] = []

    def flush():
        if run:
            out.append({"type": "image", "images": [b["src"] for b in run],
                        "caption": "\n".join([b.get("caption") or "" for b in run if b.get("caption")])})
            run.clear()

    for b in blocks:
        if b.get("type") == "image" and Path(b.get("src") or "").exists():
            run.append(b)
        else:
            flush()
            out.append(b)
    flush()
    return out


def campaign_images(camp: dict) -> list[str]:
    """Return the list of image paths that still exist for a campaign."""
    raw = camp.get("images") or "[]"
    try:
        items = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except Exception:
        items = []
    return [p for p in (items or []) if p and Path(p).exists()]


def set_campaign_images(cid: str, paths: list[str]) -> None:
    db.set_campaign_images(cid, paths)


def _maybe_finish(cid: str) -> None:
    """Mark a campaign done once it has no pending targets left in the queue.

    Self-heals stranded targets (F6): a target can be left 'pending' with no
    job queued (e.g. start_campaign skipped it because it was still claimed,
    then that job was dropped). If a pending target is neither claimed nor
    queued nor in a backoff window, re-enqueue it so the campaign can finish.
    """
    if not cid:
        return
    try:
        pend = db.pending_targets(cid)
        if pend:
            camp = db.get_campaign(cid)
            if camp and camp.get("status") == "running":
                _reconcile_queue()
                now = time.time()
                revived = 0
                for t in pend:
                    tid = t["id"]
                    with _queued_lock:
                        owned = tid in _queued_targets or tid in _busy
                    if owned or float(_next_attempt.get(tid, 0) or 0) > now:
                        continue
                    if queue_pending_target(cid, tid):
                        revived += 1
                if revived:
                    log(f"CAMPAIGN revive :: {cid} requeued {revived} stranded target(s)")
            return
        camp = db.get_campaign(cid)
        if camp and camp.get("status") in ("running", "paused"):
            db.set_campaign_status(cid, "done", finished_at=int(time.time()))
            db.log_event("campaign_done", cid, campaign_id=cid)
            log(f"CAMPAIGN done :: {cid}")
    except Exception as e:  # pragma: no cover
        log(f"finish check error: {e}")


# --------------------------------------------------------------------- groups

def group_scan_status(account_id: str) -> dict:
    """Current scan rate state for an account (for UI display)."""
    d = settings().get("defaults", {})
    now = int(time.time())
    hour = db.scan_counts(account_id, now - 3600)
    day = db.scan_counts(account_id, now - 86400)
    last = db.last_scan_ts(account_id)
    gap = int(d.get("group_scan_gap_seconds", 45)) + int(d.get("group_scan_jitter_seconds", 20)) // 2
    wait = max(0, int(last + gap - now)) if last else 0
    return {
        "hour": hour, "hour_cap": int(d.get("group_scan_per_hour", 20)),
        "day": day, "day_cap": int(d.get("group_scan_per_day", 60)),
        "last_ts": last, "wait": wait, "gap": gap,
        "can_scan": hour < int(d.get("group_scan_per_hour", 20))
                    and day < int(d.get("group_scan_per_day", 60)) and wait <= 0,
    }


def _scan_allowed(account_id: str) -> tuple[bool, str, int]:
    d = settings().get("defaults", {})
    st = group_scan_status(account_id)
    if st["day"] >= st["day_cap"]:
        return False, f"đã đạt giới hạn {st['day_cap']} lượt quét/ngày", 0
    if st["hour"] >= st["hour_cap"]:
        return False, f"đã đạt giới hạn {st['hour_cap']} lượt quét/giờ", 0
    if st["wait"] > 0:
        return False, f"vừa quét xong, chờ {st['wait']}s để tránh khóa nick", st["wait"]
    return True, "", 0


def sync_groups(account_id: str) -> dict:
    """Fetch all groups the account is in and store them."""
    if not account_online(account_id):
        return {"ok": False, "error": "nick đang tắt hoặc chưa đăng nhập"}
    d = settings().get("defaults", {})
    gap = int(d.get("group_sync_gap_seconds", 600))
    last_sync = 0
    ev = [e for e in db.recent_events(200) if e["kind"] == "groups_sync" and e["account_id"] == account_id]
    if ev:
        last_sync = ev[0]["ts"]
    if last_sync and int(time.time()) - last_sync < gap:
        return {"ok": False, "error": f"vừa đồng bộ, chờ {gap - (int(time.time()) - last_sync)}s"}
    r = sessiond_client.cmd(account_id, {"cmd": "groups"}, timeout=180)
    if not r.get("ok"):
        return {"ok": False, "error": str(r.get("error"))[:300]}
    groups = r.get("groups", [])
    n = db.upsert_groups(account_id, groups)
    db.log_event("groups_sync", f"{n} groups", account_id=account_id)
    return {"ok": True, "count": n, "groups": groups}


_scans_running: set[str] = set()
_scans_lock = threading.Lock()


def scan_group_members(account_id: str, group_id: str) -> dict:
    """Start a non-blocking group-member scan in a background thread.
    Progress + result land in db.group_scan_progress; UI polls it."""
    if not account_online(account_id):
        return {"ok": False, "error": "nick đang tắt hoặc chưa đăng nhập"}
    with _scans_lock:
        if group_id in _scans_running:
            return {"ok": False, "error": "đang quét nhóm này rồi", "running": True}
    allowed, why, retry = _scan_allowed(account_id)
    if not allowed:
        db.log_event("group_scan_blocked", f"{group_id}: {why}", account_id=account_id)
        db.scan_progress_finish(group_id, status="blocked", error=why)
        return {"ok": False, "error": why, "retry_after": retry}
    gname = next((g["name"] for g in db.list_groups(limit=2000) if g["group_id"] == group_id), group_id)
    db.scan_progress_start(group_id, account_id, gname)
    log(f"SCAN start :: {gname} ({group_id})")
    db.log_event("group_scan_start", f"{gname}: bắt đầu quét", account_id=account_id)
    t = threading.Thread(target=_scan_worker, args=(account_id, group_id, gname), daemon=True)
    t.start()
    return {"ok": True, "started": True, "group_id": group_id, "group": gname}


def _scan_worker(account_id: str, group_id: str, gname: str) -> None:
    with _scans_lock:
        _scans_running.add(group_id)
    st = group_scan_status(account_id)
    if st["wait"] > 0:
        log(f"SCAN wait :: {st['wait']}s (gap chống khóa nick)")
        db.scan_progress_update(group_id, error=f"chờ {st['wait']}s")
        time.sleep(min(st["wait"], 60))

    def on_event(ev: dict) -> None:
        et = ev.get("event")
        if et == "start":
            db.scan_progress_update(group_id, total=int(ev.get("total") or 0), fetched=0)
            log(f"SCAN info :: {gname}: {ev.get('total')} thành viên / {'?'} chunk")
        elif et == "chunk":
            fetched = int(ev.get("fetched") or 0)
            db.scan_progress_update(group_id, fetched=fetched, total=int(ev.get("total") or 0))
            log(f"SCAN chunk {ev.get('chunk')}/{ev.get('chunks')} :: lấy {fetched} member")

    try:
        def _on_scan(ev: dict) -> None:
            # sessiond streams {event:"scan", phase:"start|chunk", ...}; the
            # controller's progress handler keys off the phase.
            phase = ev.get("phase") or ev.get("event")
            if phase in ("start", "chunk"):
                on_event({**ev, "event": phase})
        final = sessiond_client.stream(
            account_id,
            {"cmd": "group-members", "groupId": group_id, "stream": True},
            _on_scan, timeout=900)
        if not final.get("ok") or final.get("event") == "error":
            msg = str(final.get("error"))[:300]
            db.scan_progress_finish(group_id, status="error", error=msg)
            db.log_event("group_scan_error", f"{gname}: {msg}", account_id=account_id)
            log(f"SCAN fail :: {gname} :: {msg}")
            return
        members = final.get("members", [])
        n = db.upsert_members(account_id, group_id, members)
        total = final.get("totalMember", n) or n
        partial = 1 if (final.get("partial") or n < total) else 0
        with db._conn() as c:
            c.execute("UPDATE groups SET member_count=?, updated_at=? WHERE account_id=? AND group_id=?",
                      (n, int(time.time()), account_id, group_id))
        db.scan_progress_finish(group_id, status="done", total=total, fetched=n, partial=partial)
        tag = "một phần" if partial else "đủ"
        db.log_event("group_scan", f"{gname}: {n}/{total} thành viên ({tag})", account_id=account_id)
        log(f"SCAN done :: {gname}: {n}/{total} ({tag})")
    except Exception as e:  # pragma: no cover
        db.scan_progress_finish(group_id, status="error", error=str(e)[:300])
        log(f"SCAN exception :: {gname} :: {e}")
    finally:
        with _scans_lock:
            _scans_running.discard(group_id)


def scan_progress(group_id: str) -> dict:
    p = db.scan_progress_get(group_id)
    if not p:
        return {"status": "none"}
    p["running"] = p.get("status") == "running" or group_id in _scans_running
    return p


# -------------------------------------------------------------- campaigns

def build_campaign_targets(cid: str) -> int:
    """Resolve pending targets' phones to zalo_user_id where possible."""
    camp = db.get_campaign(cid)
    if not camp:
        return 0
    acct = camp["account_id"] or "default"
    rows = db.pending_targets(cid)
    # Only real phone numbers need a lookup; long Zalo ids ARE the thread already.
    phones = [r["phone"] for r in rows if r["phone"] and not r["zalo_user_id"]
              and is_real_phone(r["phone"])]
    resolved = 0
    if phones and is_logged_in(acct):
        for chunk in [phones[i:i + 50] for i in range(0, len(phones), 50)]:
            r = sessiond_client.cmd(acct, {"cmd": "find-users", "phones": chunk}, timeout=180)
            if r.get("ok"):
                for u in r.get("users", []):
                    if u.get("phone"):
                        db.update_contact_resolution(u["phone"], u.get("userId"),
                                                     u.get("displayName", ""), gender=u.get("gender", ""))
                        db.set_target_resolution(cid, u["phone"], u.get("userId"))
            time.sleep(1)
        # fill any target whose phone is now known in contacts
        contacts = {c["phone"]: c for c in db.list_contacts(5000)}
        for r in rows:
            zid = contacts.get(r["phone"], {}).get("zalo_user_id")
            if zid:
                db.set_target_resolution(cid, r["phone"], zid)
    for r in db.pending_targets(cid):
        if r["zalo_user_id"]:
            resolved += 1
    return resolved


def queue_pending_target(cid: str, tid: int) -> bool:
    """(Re)build + enqueue ONE pending target's job for its campaign.

    Shared by the F6 self-heal path. Claims the target atomically; returns
    False when the target is no longer pending or the claim was lost.
    """
    camp = db.get_campaign(cid)
    if not camp:
        return False
    r = db.get_target(tid)
    if not r or r.get("campaign_id") != cid or r.get("status") != "pending":
        return False
    if not _claim_target(tid):
        return False
    acct = camp["account_id"] or "default"
    kind = camp["kind"]
    if kind == "message" and camp.get("thread_id"):
        thread, ttype = camp["thread_id"], camp.get("thread_type", "group")
    else:
        thread, ttype = (r["zalo_user_id"] or r["phone"]), "user"
    contact = db.get_contact_by_phone(r["phone"]) or {}
    nm = (contact.get("display_name") or db.display_name_for(r["phone"])
          or db.display_name_for(r["zalo_user_id"] or "") or "")
    blocks = _img_batches(compile_blocks(parse_blocks(camp), name=nm,
                                         gender=contact.get("gender") or ""))
    if not blocks:
        images = campaign_images(camp)
        if camp.get("url"):
            blocks = [{"type": "link", "url": camp["url"], "caption": ""}]
        elif images:
            blocks = [{"type": "image", "images": images, "caption": ""}]
    if not blocks:
        _release_target(tid)
        return False
    text = next((b.get("content") for b in blocks if b.get("type") == "text"), "")
    url = next((b.get("url") for b in blocks if b.get("type") == "link"), None) or camp.get("url")
    enqueue({
        "account_id": acct, "campaign_id": cid, "target_id": tid,
        "thread": thread, "type": ttype, "kind": kind,
        "blocks": blocks, "text": text, "url": url,
        "images": campaign_images(camp), "file_caption": "",
        "alias_uid": r["zalo_user_id"] or "", "alias_phone": r["phone"], "alias_name": nm,
        "dry_run": bool(camp.get("dry_run", 1)),
    }, claim=False)
    return True


def start_campaign(cid: str) -> dict:
    camp = db.get_campaign(cid)
    if not camp:
        return {"ok": False, "error": "campaign not found"}
    acct = camp["account_id"] or "default"
    kind = camp["kind"]
    # Clear any lingering pause flag and re-enqueue parked jobs so (re)starting
    # is never blocked (fix deadlock) and nothing is left stranded.
    _campaign_paused.discard(cid)
    for j in _paused_jobs.pop(cid, []):
        _JOBS.put(j)
    _reconcile_queue()  # keep the claim set truthful before claiming (F6)
    rows = [r for r in db.pending_targets(cid) if r["id"] not in _queued_targets]
    if not rows:
        if db.pending_targets(cid):
            # targets already queued/in-flight -> this is effectively a resume
            db.set_campaign_status(cid, "running")
            db.log_event("campaign_resume", cid, account_id=acct, campaign_id=cid)
            _ensure_worker()
            return {"ok": True, "resumed": True, "queued": 0}
        cnt = db.campaign_counts(cid)
        hint = ""
        if cnt.get("failed") or cnt.get("skipped"):
            hint = " — bấm '↻ Chạy lại lỗi' để thử lại các SĐT thất bại."
        return {"ok": False,
                "error": f"Không còn SĐT chờ gửi (trạng thái: {camp.get('status')})"
                         + hint}

    # Auto-fill name + gender for phone-only files (unless disabled).
    # Scope the resolve to THIS campaign's pending phones only (not the whole
    # address book) so starting stays fast even with a huge contacts table.
    auto = db.get_setting("defaults") or {}
    if auto.get("auto_resolve_contacts", True) and is_logged_in(acct):
        need = [r["phone"] for r in rows
                if not r.get("zalo_user_id") and is_real_phone(r["phone"])]
        if need:
            try:
                resolve_phones(acct, need, cid=cid)
            except Exception as e:
                log(f"auto-resolve skipped: {e}")
            rows = [r for r in db.pending_targets(cid) if r["id"] not in _queued_targets]

    # Media: campaign-level fixed photo album (same images for every target).
    # The template body is delivered as a normal text message after the album.
    images = campaign_images(camp)
    camp_blocks = parse_blocks(camp)  # ordered text/image/link blocks (ISO block list)
    if not camp_blocks and not images:
        return {"ok": False, "error": "Chiến dịch chưa có nội dung gửi — thêm khối Chữ/Ảnh/Link rồi thử lại."}

    # LIVE preflight: never start a real-send run when the account is not logged
    # in (would burn the whole campaign). Then auto-resolve phone -> userId so a
    # direct "Chạy" works without a separate Resolve step.
    if not bool(camp.get("dry_run", 1)):
        if not account_enabled(acct):
            return {"ok": False, "error": f"Tài khoản '{acct}' đang TẮT — bật lại ở tab Tài khoản rồi thử lại."}
        if not is_logged_in(acct):
            return {"ok": False, "error": f"Tài khoản '{acct}' chưa đăng nhập — không chạy LIVE được. "
                                             f"Đăng nhập QR rồi thử lại (hoặc để DRY)."}
        try:
            build_campaign_targets(cid)
            rows = [r for r in db.pending_targets(cid) if r["id"] not in _queued_targets]
        except Exception as e:
            log(f"live auto-resolve skipped: {e}")

    unresolved = sum(1 for r in rows
                     if not r["zalo_user_id"] and is_real_phone(r["phone"]))

    # Live-only cross-campaign guard: skip a recipient already messaged in a
    # LIVE campaign within the cooldown window (F4). DRY runs never consume it.
    cd_gate = 0
    if not bool(camp.get("dry_run", 1)):
        cd_gate = int((settings().get("defaults", {}) or {}).get("dedupe_sent_days", 3) or 3)

    enq = 0
    for r in rows:
        # group campaign ignores per-target id and sends to the group thread
        if kind == "message" and camp.get("thread_id"):
            thread, ttype = camp["thread_id"], camp.get("thread_type", "group")
        else:
            thread = r["zalo_user_id"] or r["phone"]
            ttype = "user"
        # F4: cross-campaign de-dup (LIVE only, skipped for group broadcasts)
        if cd_gate and ttype != "group":
            ident = r.get("ident") or db.canon_ident(r["phone"], r.get("zalo_user_id"))
            if db.sent_ident_recent(acct, ident, cd_gate):
                db.mark_target(r["id"], "skipped", error=f"đã gửi trong {cd_gate} ngày")
                continue
        # Atomic claim BEFORE enqueue: two concurrent 'Chạy' clicks can no longer
        # each enqueue the same target (F1). If the claim is lost, skip — some
        # other call already owns this target.
        if not _claim_target(r["id"]):
            continue
        enq += 1
        # personalize per recipient (name + salutation by gender)
        contact = db.get_contact_by_phone(r["phone"]) or {}
        nm = contact.get("display_name") or ""
        if not nm:
            nm = db.display_name_for(r["phone"]) or db.display_name_for(r["zalo_user_id"] or "") or ""
        gender = contact.get("gender") or ""
        # Compile the block list for THIS recipient (placeholders filled in).
        blocks = _img_batches(compile_blocks(camp_blocks, name=nm, gender=gender))
        # Never enqueue a job with no sendable content: fall back to a bare link
        # card, or the campaign's album, so a stray missing placeholder/photo
        # can't turn every recipient into a silent no-op.
        if not blocks:
            if camp.get("url"):
                blocks = [{"type": "link", "url": camp["url"], "caption": ""}]
            elif images:
                blocks = [{"type": "image", "images": images, "caption": ""}]
        # Legacy job fields kept for backward compat / dry-run logging / alias.
        text = next((b.get("content") for b in blocks if b.get("type") == "text"), "")
        url = next((b.get("url") for b in blocks if b.get("type") == "link"), None) or camp.get("url")
        enqueue({
            "account_id": acct, "campaign_id": cid, "target_id": r["id"],
            "thread": thread, "type": ttype, "kind": kind,
            "blocks": blocks,
            "text": text, "url": url,
            "images": images, "file_caption": "",
            "alias_uid": r["zalo_user_id"] or "", "alias_phone": r["phone"], "alias_name": nm,
            "dry_run": bool(camp.get("dry_run", 1)),
        }, claim=False)
    db.set_campaign_status(cid, "running", started_at=int(time.time()))
    db.log_event("campaign_start", cid + (f" unresolved={unresolved}" if unresolved else ""),
                 account_id=acct, campaign_id=cid)
    if rows:
        log(f"campaign {cid}: queued {enq}/{len(rows)} · {blocks_summary(camp_blocks)}")
    _ensure_worker()
    resp = {"ok": True, "queued": enq}
    if unresolved:
        resp["warn"] = (f"{unresolved} SĐT chưa tra được — bấm 'Resolve SĐT' trước để tránh gửi nhầm/nhỡ.")
    return resp


def new_campaign(**kw) -> str:
    cid = kw.get("id") or ("c_" + uuid.uuid4().hex[:8])
    kw["id"] = cid
    db.create_campaign(cid, **kw)
    return cid


# =========================================================== autobot (A -> B)
# Listen-ONLY forwarding: for each enabled rule we watch a source group (A) on
# one Zalo account and relay each NEW message a human posts there into a
# destination group (B). There is NO auto-reply: the bot never answers anyone,
# it only re-posts A's messages into B. The listener runs inside the shared
# per-account sessiond daemon (bridge/sessiond.mjs); everything respects the
# global KILLSWITCH and each rule's DRY-RUN switch / rate caps.

_bot_lock = threading.Lock()
_bots: dict[str, dict] = {}          # account_id -> {proc, ready, out, ...}
_bot_seen: dict[str, float] = {}      # account_id -> ts of last message seen
_bot_hb: dict[str, float] = {}        # account_id -> ts of last socket heartbeat
_bot_dedup: dict[tuple, float] = {}   # L1 in-memory cache: (account, group, key) -> ts
_no_msgid_count: dict[str, int] = {}  # account_id -> events missing msgId (observability)
_fwd_last: dict[str, float] = {}      # rule_id -> ts of last forward (for gap)


def _fwd_msg_key(ev: dict) -> str:
    """Stable dedup key for a source message.

    Prefers the Zalo id (msgId, else realMsgId); falls back to a content hash
    when the event carries no id at all, so such messages are still deduped.
    """
    mid = str(ev.get("msgId") or "").strip() or str(ev.get("realMsgId") or "").strip()
    if mid:
        return "m:" + mid
    raw = f'{ev.get("uidFrom", "")}|{ev.get("ts", "")}|{ev.get("text", "")}'
    return "h:" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def _bot_watch_groups(account_id: str) -> list[str]:
    """Source groups this account should listen to = src_group_id of every
    ENABLED rule for the account. This is what makes the pipeline "only listen
    to the groups added to it"."""
    out: list[str] = []
    seen: set[str] = set()
    for r in db.list_forward_rules():
        if r.get("account_id") != account_id or not r.get("enabled"):
            continue
        gid = str(r.get("src_group_id") or "").strip()
        if gid and gid not in seen:
            seen.add(gid)
            out.append(gid)
    return out


def _bot_apply_watch(account_id: str) -> dict:
    """Push the current watch allowlist to a running worker (no restart)."""
    groups = _bot_watch_groups(account_id)
    mode = "only" if groups else "all"
    res = _bot_cmd(account_id, {"cmd": "set-watch", "mode": mode, "groups": groups}, timeout=10)
    bot = _bots.get(account_id)
    if bot is not None:
        bot["watch"] = {"mode": mode, "count": len(groups)}
    return res


def _bot_backfill_cursors(account_id: str) -> dict:
    """Per-group history cursor for a (re)connect backfill.

    Prefer each rule's durable `last_seen_msg_id` (the exact cut-off: replay only
    what came after it). If a rule never saw a live message yet (fresh rule /
    fresh restart), fall back to the newest message the controller has already
    stored for that source group, so a restart does not re-deliver the whole group.
    """
    cursors: dict[str, str] = {}
    try:
        for r in db.list_forward_rules():
            if not r.get("enabled") or r.get("account_id") != account_id:
                continue
            gid = str(r.get("src_group_id") or "")
            if not gid:
                continue
            cur = str(r.get("last_seen_msg_id") or "").strip()
            if not cur:
                try:
                    cur = str(db.last_forward_src_msg_id(r["id"], gid) or "").strip()
                except Exception:
                    cur = ""
            if cur:
                cursors[gid] = cur
    except Exception as e:
        log(f"AUTOBOT backfill cursors error :: {e!r}")
    return cursors


def bot_backfill(account_id: str, cursor_override: str = "") -> dict:
    """Ask a running worker to replay missed history (idempotent).

    `cursor_override` (operator/testing) replaces every group cursor with the
    given msgId so history from that point is replayed.
    """
    _bot_apply_watch(account_id)
    cursors = _bot_backfill_cursors(account_id)
    ov = str(cursor_override or "").strip()
    if ov:
        cursors = {g: ov for g in _bot_watch_groups(account_id)}
    return _bot_cmd(account_id, {"cmd": "backfill", "cursors": cursors}, timeout=30)


def _bot_creds_ok(account_id: str) -> bool:
    return account_creds_path(account_id).exists()


def _bot_cmd(account_id: str, obj: dict, timeout: float = 30.0) -> dict:
    """Send one JSON command to the shared session daemon and wait for its reply."""
    try:
        return sessiond_client.cmd(account_id, obj, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": f"sessiond: {e}"}


def _bot_reader(account_id: str) -> None:
    """Lazy-start the shared session daemon and wire its events into the G2G
    controller. The daemon may already be running (owned by this or another app),
    in which case we simply attach to the existing socket."""
    bot = _bots.get(account_id)
    if not bot:
        return
    try:
        groups = _bot_watch_groups(account_id)
        spec = ("only:" + ",".join(groups)) if groups else "all"
        conn = sessiond_client.session.ensure(account_id, watch_spec=spec, wait=40)

        def on_event(ev: dict) -> None:
            kind = ev.get("event")
            if kind in ("ready", "hello"):
                if kind == "hello" and not ev.get("ready"):
                    return
                bot["own_id"] = ev.get("ownId") or bot.get("own_id", "")
                w = ev.get("watch") or {}
                bot["watch"] = {"mode": w.get("mode", "all"), "count": w.get("count", 0)}
                first = not bot["ready"].is_set()
                bot["ready"].set()
                if first:
                    log(f"AUTOBOT ready :: {account_id} (own {bot['own_id']})")
                    threading.Thread(target=_bot_apply_watch, args=(account_id,), daemon=True).start()
            elif kind == "msg":
                try:
                    _bot_handle_message(account_id, ev)
                except Exception as e:  # never die on one bad message
                    log(f"AUTOBOT msg error :: {e!r}")
            elif kind in ("listen-error", "listen-disconnected", "msg-error"):
                bot["error"] = str(ev.get("error") or ev.get("reason") or kind)[:300]
                bot["err_ts"] = time.time()
                log(f"AUTOBOT {kind} :: {account_id} :: {bot['error']}")
            elif kind == "listen-connected":
                bot["error"] = ""
                bot.pop("err_ts", None)
            elif kind == "heartbeat":
                _bot_hb[account_id] = time.time()
                with _bot_lock:
                    b2 = _bots.get(account_id)
                    if b2 is not None:
                        b2["hb"] = _bot_hb[account_id]
            elif kind == "backfill":
                _bot_hb[account_id] = time.time()
                log(f"AUTOBOT backfill :: {account_id} :: quét {ev.get('scanned', 0)} "
                    f"phát lại {ev.get('restored', 0)} ({ev.get('reason', '')})")
            elif kind == "media-error":
                log(f"AUTOBOT media-error :: {account_id} :: {str(ev.get('error'))[:200]}")
            elif kind == "fatal":
                bot["error"] = str(ev.get("error") or "daemon fatal")[:300]
                bot["err_ts"] = time.time()
                log(f"AUTOBOT daemon-fatal :: {account_id} :: {bot['error']}")

        conn.subscribe(on_event)
        bot["conn"] = conn
        # A client that attaches to an ALREADY-booted daemon misses the boot-time
        # `ready` event (it fired before we subscribed). Probe status once so the
        # registration reflects reality.
        try:
            st = conn.cmd({"cmd": "status"}, timeout=8)
            if st.get("ok"):
                bot["own_id"] = st.get("ownId") or bot.get("own_id", "")
                w = st.get("watch") or {}
                bot["watch"] = {"mode": w.get("mode", "all"), "count": w.get("count", 0)}
                if st.get("ready"):
                    first = not bot["ready"].is_set()
                    bot["ready"].set()
                    if first:
                        log(f"AUTOBOT ready :: {account_id} (own {bot['own_id']}, attach)")
        except Exception:
            pass
        # Replay history missed while the socket was down (idempotent via ledger).
        threading.Thread(target=bot_backfill, args=(account_id,), daemon=True).start()
    except Exception as e:  # pragma: no cover
        bot["error"] = str(e)[:300]
        log(f"AUTOBOT reader start error :: {account_id} :: {e!r}")

def _bot_dedup_ok(account_id: str, group: str, msg_key: str) -> bool:
    """Two-tier duplicate guard.

    L1: in-memory cache (fast, bounded LRU-ish, 1h TTL).
    L2: durable `forward_seen` ledger (restart-safe). A hit in either tier means
    the message was already handled. Empty keys are treated as unique by the
    caller (see _fwd_msg_key) which now always produces a non-empty key.
    """
    if not msg_key:
        return True
    now = time.time()
    key = (account_id, group, msg_key)
    # L1: memory cache
    hit = _bot_dedup.get(key)
    if hit is not None and now - hit <= 3600:
        return False
    _bot_dedup[key] = now
    # prune L1
    if len(_bot_dedup) > 5000:
        cutoff = now - 3600
        for k in [k for k, t in _bot_dedup.items() if t < cutoff]:
            _bot_dedup.pop(k, None)
    # L2: durable ledger (survives restart / replay on reconnect).
    # 30-day retention: long enough that a reconnect backfill never re-delivers
    # an old post, bounded enough that the table stays small.
    try:
        return db.seen_forward(account_id, group, msg_key, ttl_seconds=30 * 86400)
    except Exception as e:  # never fail closed on a dedup backend error
        log(f"AUTOBOT dedup-ledger error :: {e!r}")
        return True


def _bot_handle_message(account_id: str, ev: dict) -> None:
    """A new message arrived on account_id. Match it against enabled rules."""
    _bot_seen[account_id] = time.time()
    gid = str(ev.get("group") or "")
    if not gid:
        return  # autobot relays GROUP -> GROUP only (ignores 1:1 chats)
    text = (ev.get("text") or "").strip()
    msg_id = str(ev.get("msgId") or "")
    msg_key = _fwd_msg_key(ev)
    part = ev.get("part") if isinstance(ev.get("part"), dict) else None
    if not msg_id:
        _no_msgid_count[account_id] = _no_msgid_count.get(account_id, 0) + 1
        if _no_msgid_count[account_id] % 50 == 1:
            log(f"AUTOBOT no-msgid :: {account_id} :: hash-fallback "
                f"(#{_no_msgid_count[account_id]})")
    for rule in db.list_forward_rules():
        if not rule.get("enabled") or rule.get("account_id") != account_id:
            continue
        if str(rule.get("src_group_id")) != gid:
            continue
        if ev.get("isSelf") and not rule.get("include_self"):
            continue
        if not _bot_dedup_ok(account_id, gid, msg_key):
            continue
        media = [u for u in (ev.get("media") or []) if isinstance(u, str) and u.startswith("http")]
        media_kind = str(ev.get("mediaKind") or "")
        if not part:
            # back-compat: derive a part when the bridge didn't send one
            if media:
                part = {"kind": media_kind or "image", "url": media[0], "caption": text}
            elif text:
                part = {"kind": "text", "text": text}
        if not text and not media and not part:
            db.bump_forward_rule(rule["id"], skipped=1)
            db.add_forward_log(rule["id"], gid, ev.get("name", ""), "(không phải tin chữ)",
                               "skip:rỗng", src_msg_id=msg_id)
            continue
        if msg_id:
            db.bump_forward_rule(rule["id"], last_seen_msg_id=msg_id)
        _bot_dispatch_part(rule, ev, part, text, gid, msg_id, media, media_kind)


# ---- grouping buffer: coalesce an image + its follow-up text into 1 post ----
_fwd_buf_lock = threading.Lock()
_fwd_buf: dict[str, dict] = {}   # key -> {rule,parts,gid,name,msgs,timer,epoch}
_fwd_buf_epoch = 0


def _reply_quote_text(quote: dict) -> str:
    """Human one-liner for the message a reply points at."""
    mt = str(quote.get("msgType") or "")
    msg = (quote.get("msg") or "").strip()
    if msg:
        s = re.sub(r"\s+", " ", msg)
        return s[:120] + ("…" if len(s) > 120 else "")
    try:
        attach = quote.get("attach")
        a = json.loads(attach) if isinstance(attach, str) and attach.strip() else (attach or {})
    except Exception:
        a = {}
    if not isinstance(a, dict):
        a = {}
    t = (a.get("title") or "").strip()
    if t:
        s = re.sub(r"\s+", " ", t)
        return s[:120] + ("…" if len(s) > 120 else "")
    if a.get("href") or a.get("thumbUrl") or a.get("oriUrl"):
        return "[hình ảnh]"
    ct = int(quote.get("cliMsgType") or 0)
    label = {31: "[thoại]", 32: "[hình ảnh]", 36: "[nhãn dán]", 37: "[nét vẽ]",
             38: "[liên kết]", 43: "[vị trí]", 44: "[video]", 46: "[tệp tin]", 49: "[GIF]"}.get(ct)
    if mt == "chat.voice" or label == "[thoại]":
        label = "[thoại]"
    return label or "[nội dung khác]"


def _quote_dst_map(rule_id: str, account_id: str, gid: str, global_id: str) -> tuple[str, str]:
    """For a reply's parent, return (dst_msg_id, dst_cliMsgId) so the bridge can
    build a NATIVE quote in the destination group. ('', '') when not mapped.

    The parent's source msgId equals our stored `dst_msg_id` whenever the copy
    was sent by an account we control (the bridge echoes the real msgId), so a
    direct msgId is enough in the common case.
    """
    if not global_id:
        return "", ""
    try:
        row = db.find_forward_by_src(rule_id, gid, global_id)
    except Exception:
        row = None
    if not row:
        return "", ""
    dst_child = str(row.get("dst_msg_id") or "").strip()
    if dst_child.isdigit():
        return dst_child, ""
    return "", ""


def _reply_prefix(quote: dict, rule: dict, rule_id: str, account_id: str, gid: str) -> tuple[str, dict | None]:
    """Build the '↩ ...' context block for a reply and (optionally) a native
    quote object. Returns (block_text, quote_obj_or_None).

    reply_mode: 'text' (default) -> context line only;
                'quote' -> native quote when mapped, else falls back to text;
                'skip'  -> caller drops the reply before calling this.
    """
    if not quote:
        return "", None
    mode = str(rule.get("reply_mode") or "text")
    qline = _reply_quote_text(quote)
    tsline = ""
    if int(rule.get("reply_quote_ms") or 0) and quote.get("ts"):
        try:
            qt = time.localtime(float(quote["ts"]) / 1000.0)
            tsline = time.strftime("%H:%M:%S", qt)
            # A reply may point at a message from an EARLIER day; then the clock
            # alone is ambiguous. Prefix dd/mm/YYYY only when the parent is not
            # from today (same-day replies keep the compact hh:mm:ss form).
            today = time.localtime()
            if (qt.tm_year, qt.tm_yday) != (today.tm_year, today.tm_yday):
                tsline = time.strftime("%d/%m/%Y ", qt) + tsline
        except Exception:
            tsline = ""
    # NOTE: name of the original sender is intentionally NOT included in the
    # context line sent to group B (per operator request).
    block = f"↩ {tsline}: {qline}".rstrip() if tsline else f"↩ {qline}".rstrip()
    if mode == "quote":
        gid_parent = str(quote.get("globalMsgId") or "")
        dst_msg, dst_cli = _quote_dst_map(rule_id, account_id, gid, gid_parent)
        if dst_msg:
            try:
                tsu = int(float(quote["ts"]) / 1000.0)
            except Exception:
                tsu = int(time.time())
            content = quote.get("msg")
            if not isinstance(content, str) or not content.strip():
                content = qline
            qobj = {
                "uidFrom": str(quote.get("ownerId") or "") or str(rule.get("account_id") or ""),
                "msgId": dst_msg,
                "msgType": str(quote.get("msgType") or "webchat"),
                "cliMsgId": dst_cli,
                "propertyExt": {},
                "ts": tsu,
                "ttl": 0,
                "content": content or "",
            }
            return block, qobj
    return block, None


def _bot_dispatch_part(rule: dict, ev: dict, part: dict | None, text: str, gid: str,
                       msg_id: str, media: list, media_kind: str) -> None:
    """Either enqueue immediately (window=0) or buffer the part for `window` ms
    so a photo and the text sent right after it are delivered as one post."""
    global _fwd_buf_epoch
    window = int(rule.get("group_window_ms") or 0)
    # Reply handling: 'skip' drops replies here; otherwise append a context
    # line (and, in 'quote' mode, a native quote object) to the post.
    rq = ev.get("quote") if isinstance(ev.get("quote"), dict) else None
    rprefix, rquote = "", None
    if rq:
        mode = str(rule.get("reply_mode") or "text")
        if mode == "skip":
            db.add_forward_log(rule["id"], gid, ev.get("name", ""), text, "skip:reply",
                               src_msg_id=msg_id)
            db.bump_forward_rule(rule["id"], skipped=1)
            return
        rprefix, rquote = _reply_prefix(rq, rule, rule["id"], rule["account_id"], gid)
    base = {"rule_id": rule["id"], "account_id": rule["account_id"],
            "src_group_id": gid, "src_name": ev.get("name", ""),
            "dst_group_id": rule["dst_group_id"], "src_msg_id": msg_id,
            "media": media, "media_kind": media_kind}
    if rprefix:
        base["reply_prefix"] = rprefix
        base["quote"] = rquote
    if window <= 0 or not part:
        base["parts"] = [part] if part else []
        base["text"] = text
        _bot_enqueue(base)
        return
    key = f"{rule['id']}|{gid}|{ev.get('uidFrom') or ''}"
    with _fwd_buf_lock:
        slot = _fwd_buf.get(key)
        if slot is None or slot["timer"] is None:
            slot = {"rule": rule, "base": base, "parts": [], "msgs": [],
                    "timer": None, "epoch": _fwd_buf_epoch}
            _fwd_buf[key] = slot
        if part:
            slot["parts"].append(part)
        if msg_id:
            slot["msgs"].append(msg_id)
        if slot["timer"] is not None:
            slot["timer"].cancel()
        t = threading.Timer(window / 1000.0, _bot_flush, args=(key,))
        t.daemon = True
        slot["timer"] = t
        t.start()


def _emit_slot(slot: dict) -> None:
    """Build the post from a buffered slot and hand it to the sender."""
    if not slot or slot.get("epoch") != _fwd_buf_epoch:
        return  # system was stopped/restarted since buffered
    base = dict(slot["base"])
    base["parts"] = slot["parts"]
    base["text"] = " ".join(p.get("text") or p.get("caption") or "" for p in slot["parts"]).strip()
    msgs = [m for m in (slot.get("msgs") or []) if m]
    base["src_msg_id"] = msgs[-1] if msgs else base.get("src_msg_id", "")
    if len(msgs) > 1:
        # Several source messages were grouped into this one post. Remember the
        # others so the sender can write an audit row per source id — the log
        # would otherwise show 1 row for N received messages (the "đếm hụt"
        # seen in the audit).
        base["extra_src_ids"] = [m for m in msgs if m != base["src_msg_id"]]
    _bot_enqueue(base)


def _bot_flush(key: str) -> None:
    global _fwd_buf_epoch
    with _fwd_buf_lock:
        slot = _fwd_buf.pop(key, None)
    if not slot:
        return
    _emit_slot(slot)


def _bot_flush_all() -> None:
    """Flush every buffered post (called on stop/killswitch)."""
    global _fwd_buf_epoch
    with _fwd_buf_lock:
        keys = list(_fwd_buf.keys())
        _fwd_buf_epoch += 1  # invalidate any still-running timers
    for k in keys:
        _bot_flush(k)


def _bot_enqueue(item: dict) -> None:
    bot = _bots.get(item.get("account_id") or "")
    if not bot:
        db.add_forward_log(item["rule_id"], item.get("src_group_id", ""),
                           item.get("src_name", ""), item.get("text", ""),
                           "skip: bot tắt", src_msg_id=item.get("src_msg_id", ""))
        db.bump_forward_rule(item["rule_id"], skipped=1)
        return
    bot["out"].put(item)


def _fwd_send_cmd(dst_group: str, text: str, parts: list | None, prefix: str, dry: bool,
                  reply_prefix: str = "", quote: dict | None = None) -> dict:
    """Build the bridge `send` command.

    Canonical `parts` (text/image/video/file/voice/sticker/link) are handed to
    the bridge, which coalesces them and builds ordered send-blocks — one
    source of truth for how a multi-part post becomes Zalo messages. The rule
    `prefix` rides the FIRST block only. Falls back to a plain text send when
    there are no parts (legacy path).

    A reply adds `reply_prefix` (a "↩ [date ]time: quote" context line, merged
    into the first send-block and appended BELOW the body, one blank line apart)
    and, in native-quote mode, a `quote` object the bridge turns into a real
    Zalo quote.
    """
    parts = [p for p in (parts or []) if isinstance(p, dict)]
    rp = (reply_prefix or "").strip()
    base: dict = {"cmd": "send", "thread": str(dst_group), "type": "group", "dryRun": bool(dry)}
    if rp:
        base["reply_prefix"] = rp
    if quote:
        base["quote"] = quote
    if not parts:
        t = (text or "")
        full = (rp + "\n" + t) if rp else t
        base["text"] = (prefix or "") + full
        return base
    base["parts"] = parts
    base["prefix"] = prefix or ""
    return base


def _bot_sender_loop(account_id: str) -> None:
    bot = _bots.get(account_id)
    if not bot:
        return
    q = bot["out"]
    while True:
        item = q.get()
        if item is None:
            break
        try:
            rule = db.get_forward_rule(item["rule_id"])
            if not rule or not rule.get("enabled"):
                continue
            if killswitch_active():
                db.add_forward_log(rule["id"], item["src_group_id"], item["src_name"],
                                   item["text"], "skip: killswitch",
                                   src_msg_id=item.get("src_msg_id", ""))
                db.bump_forward_rule(rule["id"], skipped=1)
                continue
            now = time.time()
            if db.count_forwarded_since(rule["id"], int(now) - 3600) >= int(rule.get("max_per_hour") or 60):
                db.add_forward_log(rule["id"], item["src_group_id"], item["src_name"],
                                   item["text"], "skip: vượt hạn/giờ",
                                   src_msg_id=item.get("src_msg_id", ""))
                db.bump_forward_rule(rule["id"], skipped=1, error="vượt hạn/giờ")
                continue
            gap = int(rule.get("gap_seconds") or 5)
            wait = gap - (now - _fwd_last.get(rule["id"], 0))
            if wait > 0:
                time.sleep(min(wait, gap))
            prefix = rule.get("prefix") or ""
            text = (item.get("text") or "")
            parts = item.get("parts") or []
            media = [u for u in (item.get("media") or []) if isinstance(u, str) and u.startswith("http")]
            dry = bool(rule.get("dry_run"))
            rprefix = item.get("reply_prefix") or ""
            rquote = item.get("quote") or None
            cmd = _fwd_send_cmd(str(rule["dst_group_id"]), text, parts, prefix, dry,
                                reply_prefix=rprefix, quote=rquote)
            res = _bot_cmd(account_id, cmd, timeout=60)
            _fwd_last[rule["id"]] = time.time()
            ok = bool(res.get("ok"))
            status = "dry" if (ok and dry) else ("sent" if ok else "error")
            log_text = (prefix + text) if text else (
                f"[{len(res.get('msgIds') or [])} phần]" if parts else
                (f"[{len(media)} phương tiện]" if media else ""))
            written = db.add_forward_log(rule["id"], item["src_group_id"], item["src_name"], log_text,
                                         status, res.get("msgId", ""),
                                         src_msg_id=item.get("src_msg_id", ""),
                                         error=("" if ok else str(res.get("error") or "lỗi gửi")))
            if not written:
                # DB-level unique guard caught a duplicate the ledger missed
                log(f"AUTOBOT dup-db :: {rule['id']} :: {item['src_group_id']} "
                    f"msg={item.get('src_msg_id', '')}")
                continue
            db.bump_forward_rule(rule["id"], forwarded=(1 if (ok and not dry) else 0),
                                 skipped=(0 if ok else 1),
                                 error=("" if ok else str(res.get("error") or "lỗi gửi")))
            # Audit rows for the OTHER source messages grouped into this post, so
            # the forward log keeps a 1:1 trail with what the source group sent.
            for ex in (item.get("extra_src_ids") or []):
                try:
                    db.add_forward_log(rule["id"], item["src_group_id"], item["src_name"],
                                       "(gộp trong 1 bài)", status,
                                       dst_msg_id=res.get("msgId", ""), src_msg_id=ex,
                                       error=("" if ok else str(res.get("error") or "lỗi gửi")))
                except Exception:
                    pass
            kinds = ",".join((p.get("kind") or "?") for p in parts)
            log(f"AUTOBOT {status} :: {rule['id']} :: {item['src_group_id']} -> {rule['dst_group_id']}"
                f" :: {(log_text or '')[:50]}"
                + (f" [{kinds}]" if parts else (f" [{len(media)} media]" if media else "")))
        except Exception as e:  # pragma: no cover
            log(f"AUTOBOT sender error :: {e!r}")


def _bot_socket_alive(account_id: str) -> bool:
    """True when the worker's SOCKET is believed alive (recent heartbeat).

    Distinct from "a message arrived": a hb is emitted on a fixed interval while
    the listener is connected, so a long-quiet source group stays healthy instead
    of being mistaken for a dead socket."""
    hb = _bot_hb.get(account_id, 0)
    return hb > 0 and (time.time() - hb) < 180


def bot_status() -> list[dict]:
    out = []
    with _bot_lock:
        for aid, b in _bots.items():
            conn = b.get("conn")
            alive = bool(conn is not None and conn.ready)
            out.append({
                "account_id": aid, "running": alive, "ready": b["ready"].is_set(),
                "own_id": b.get("own_id", ""), "started": b.get("started"),
                "last_msg": _bot_seen.get(aid), "error": b.get("error", ""),
                "queue": b["out"].qsize(),
                "watch": b.get("watch") or {"mode": "all", "count": 0},
                "socket_alive": _bot_socket_alive(aid),
                "last_hb": _bot_hb.get(aid),
            })
    return out


def bot_for(account_id: str) -> dict | None:
    with _bot_lock:
        b = _bots.get(account_id)
        if not b:
            return None
        conn = b.get("conn")
        return {"running": bool(conn is not None and conn.ready),
                "ready": b["ready"].is_set(),
                "own_id": b.get("own_id", ""), "error": b.get("error", ""),
                "last_msg": _bot_seen.get(account_id), "queue": b["out"].qsize()}


def start_bot(account_id: str) -> dict:
    """Start (or reuse) the G2G subscription on the shared session daemon.

    The daemon itself is spawned lazily by `sessiond_client.session.ensure`; this
    only registers the controller-side listener/sender for one account. Idempotent.
    """
    account_id = (account_id or "").strip()
    if not role_allows_g2g():
        return {"ok": False, "error": "tiến trình này không chạy G2G (ZS_ROLE)"}
    if not account_id:
        return {"ok": False, "error": "thiếu account"}
    if not account_enabled(account_id):
        return {"ok": False, "error": "nick đang tắt trong panel"}
    if not _bot_creds_ok(account_id):
        return {"ok": False, "error": "nick chưa đăng nhập (thiếu credentials)"}
    with _bot_lock:
        b = _bots.get(account_id)
        if b and b.get("conn") is not None and b["conn"].ready:
            return {"ok": True, "already": True, "account": account_id}
        groups = _bot_watch_groups(account_id)
        bot = {"account": account_id, "started": time.time(),
               "ready": threading.Event(), "error": "", "own_id": "", "out": queue.Queue(),
               "conn": None,
               "watch": {"mode": ("only" if groups else "all"), "count": len(groups)}}
        _bots[account_id] = bot
        threading.Thread(target=_bot_reader, args=(account_id,), daemon=True).start()
        threading.Thread(target=_bot_sender_loop, args=(account_id,), daemon=True).start()
    ok = bot["ready"].wait(40)
    db.log_event("autobot_start", f"{account_id}", account_id=account_id)
    watch_n = bot.get("watch", {}).get("count", 0)
    watch_mode = bot.get("watch", {}).get("mode", "all")
    log(f"AUTOBOT start :: {account_id}{'' if ok else ' (chưa ready)'} "
        f"[watch {watch_mode} {watch_n}]")
    return {"ok": True, "account": account_id, "ready": ok,
            "watch": bot.get("watch")}


def stop_bot(account_id: str) -> dict:
    account_id = (account_id or "").strip()
    with _bot_lock:
        b = _bots.pop(account_id, None)
    if not b:
        return {"ok": True, "already": True}
    try:
        _bot_flush_all()
    except Exception:
        pass
    try:
        b["out"].put(None)  # stop this account's sender loop
    except Exception:
        pass
    conn = b.get("conn")
    if conn is not None:
        try:
            conn._subs.clear()
        except Exception:
            pass
    # NOTE: the session daemon is SHARED with campaign sends, so we keep it alive;
    # stopping G2G only detaches this controller-side listener.
    db.log_event("autobot_stop", f"{account_id}", account_id=account_id)
    log(f"AUTOBOT stop :: {account_id}")
    return {"ok": True, "account": account_id}


def restart_bot(account_id: str) -> dict:
    stop_bot(account_id)
    return start_bot(account_id)


def _bot_health_monitor() -> None:
    """Self-heal loop: keep enabled rules' listeners actually listening.

    `ready` only means the worker answered once; the socket can later die with a
    NORMAL_CLOSURE the bridge can no longer retry, leaving the process alive but
    deaf. We also occasionally restart to refresh session health. We do NOT
    restart while work is queued or freshly active (avoid cutting a live send).

    Liveness is judged from the SOCKET heartbeat (`_bot_hb`), not from "a message
    arrived", so a quiet source group is not mistaken for a dead socket.
    """
    QUIET_S = 6 * 3600      # refresh a listener quiet this long (session hygiene)
    HB_STALE_S = 180        # no heartbeat for this long => socket presumed dead
    TICK = 60
    while True:
        try:
            accts = {r["account_id"] for r in db.list_forward_rules()
                     if r.get("enabled") and r.get("account_id")}
            now = time.time()
            for aid in accts:
                with _bot_lock:
                    b = _bots.get(aid)
                    conn = b.get("conn") if b else None
                    alive = bool(conn is not None and conn.ready)
                    queued = b["out"].qsize() if b else 0
                    started = (b.get("started") or now) if b else now
                uptime = now - started
                last_msg = _bot_seen.get(aid) or 0
                quiet = (now - last_msg) if last_msg else 0
                hb = _bot_hb.get(aid) or 0
                stale_hb = (hb == 0) or (now - hb > HB_STALE_S)
                # don't cut a live send that is mid-flight or still grouping
                buf = sum(1 for k in _fwd_buf if k.startswith(aid + "|"))
                if not alive:
                    log(f"AUTOBOT health :: {aid} worker chết → restart")
                    restart_bot(aid)
                elif stale_hb and uptime > 120 and buf == 0:
                    # No heartbeat at all (socket dead while process stays up) →
                    # restart. Grace of 120s avoids churning a just-started
                    # worker before its first heartbeat/backfill lands.
                    log(f"AUTOBOT health :: {aid} socket im (hb {int(now - hb) if hb else -1}s) "
                        f"→ restart")
                    restart_bot(aid)
                elif quiet > QUIET_S and queued == 0 and buf == 0:
                    # Socket healthy but the source has been silent for very long:
                    # refresh for session hygiene + a free history backfill.
                    log(f"AUTOBOT health :: {aid} im nguồn {int(quiet)}s → refresh listener")
                    restart_bot(aid)
                else:
                    # live worker stuck in a reconnect/error state for too long
                    err_ts = 0
                    with _bot_lock:
                        b2 = _bots.get(aid)
                        if b2:
                            err_ts = b2.get("err_ts") or 0
                    if err_ts and (now - err_ts) > 600 and queued == 0 and buf == 0:
                        log(f"AUTOBOT health :: {aid} kẹt lỗi {int(now - err_ts)}s → restart")
                        restart_bot(aid)
        except Exception as e:  # never die
            log(f"AUTOBOT health error :: {e!r}")
        time.sleep(TICK)


_MONITOR_STARTED = False


def start_health_monitor() -> None:
    global _MONITOR_STARTED
    if _MONITOR_STARTED:
        return
    # G2G liveness/refresh is owned solely by the G2G role; the campaign role
    # must not restart/refresh listeners (that would fight the G2G process).
    if not role_allows_g2g():
        return
    _MONITOR_STARTED = True
    threading.Thread(target=_bot_health_monitor, daemon=True, name="bot-health").start()


def ensure_bots() -> dict:
    """Start bots for every account that has at least one enabled rule."""
    if not role_allows_g2g():
        return {"ok": True, "started": [], "skipped": "role"}
    started = []
    accts = {r["account_id"] for r in db.list_forward_rules() if r.get("enabled") and r.get("account_id")}
    for aid in accts:
        with _bot_lock:
            b = _bots.get(aid)
            running = bool(b and b.get("conn") is not None and b["conn"].ready)
        if not running:
            r = start_bot(aid)
            if r.get("ok"):
                started.append(aid)
    return {"ok": True, "started": started}


def autobot_snapshot() -> dict:
    rules = db.list_forward_rules()
    bots = {b["account_id"]: b for b in bot_status()}
    for r in rules:
        r["bot"] = bots.get(r["account_id"]) or {"running": False, "ready": False, "account_id": r["account_id"]}
        r["sent_last_hour"] = db.count_forwarded_since(r["id"], int(time.time()) - 3600)
    return {"rules": rules, "bots": list(bots.values()), "kill": killswitch_active()}
