"""Engine for Pipeline G2G-S01 (Signal Commodities Daily).

Mirrors the G2G autobot engine (webapp/core.py) but is scoped to the
"<NHOM_NGUON_B>" source group and adds three behaviours:

  1. REPLY in A  -> find the copy we forwarded into B and reply to THAT message
                    id (native Zalo quote) using our own s01_log mapping; falls
                    back to a "↩ <time>: <text>" context line when unmapped.
  2. @All in A   -> forward "@All <body>" into B with a REAL @All mention (the
                    original @All token text is stripped so it is not duplicated).
  3. @Name in A  -> skipped by default (rule.fw_name = 0). "Tạm thời không
                    chuyển tiếp."

The module is fully additive: it owns its own tables (pgs01_db), its own
in-process worker state, and attaches to the SAME per-account session daemon as
G2G so there is never a second Zalo session on the account. Nothing in core.py's
G2G logic is modified.
"""
from __future__ import annotations

import queue
import re
import threading
import time

import core
import db
import pgs01_db
import sessiond_client

_BOTS: dict[str, dict] = {}
_BOT_LOCK = threading.RLock()
_SEEN: dict[tuple[str, str, str], float] = {}
_HB: dict[str, float] = {}
_LAST: dict[str, float] = {}
_SEEN_ACTIVITY: dict[str, float] = {}
_MONITOR_STARTED = False

# ---- Additive: album coalescing (consecutive captionless images) ----------
# Zalo delivers a multi-image post as N SEPARATE events (one URL each), each
# ~100ms apart, so without coalescing S01 emits N separate messages into B.
# We buffer captionless images from ONE sender within the rule's
# `group_window_ms` of quiet and emit them as a SINGLE album part (urls[]),
# which sendBlocks sends as ONE Zalo message carrying multiple attachments.
# `group_window_ms <= 0` disables coalescing (legacy 1-part-per-image).
_ALBUM: dict[tuple[str, str, str], dict] = {}   # (rule_id, group, sender) -> pending
_ALBUM_LOCK = threading.RLock()
_ALBUM_REAPER_STARTED = False


# ------------------------------------------------------------------ helpers

def _s01_watch_groups(account_id: str) -> list[str]:
    """Source groups this account should listen to for S01 rules."""
    out: list[str] = []
    seen: set[str] = set()
    for r in pgs01_db.list_rules():
        if r.get("account_id") != account_id or not r.get("enabled"):
            continue
        gid = str(r.get("src_group_id") or "").strip()
        if gid and gid not in seen:
            seen.add(gid)
            out.append(gid)
    return out


def _s01_apply_watch(account_id: str) -> dict:
    """Push the account's GLOBAL watch allowlist to the running worker.

    The allowlist is the union of G2G and S01 source groups (one daemon serves
    both pipelines), so we ask core for the shared spec."""
    groups = core._bot_watch_groups(account_id)
    mode = "only" if groups else "all"
    res = core._bot_cmd(account_id, {"cmd": "set-watch", "mode": mode, "groups": groups}, timeout=10)
    bot = _BOTS.get(account_id)
    if bot is not None:
        bot["watch"] = {"mode": mode, "count": len(groups)}
    return res


def _creds_ok(account_id: str) -> bool:
    return core.account_creds_path(account_id).exists()


def _msg_key(ev: dict) -> str:
    return core._fwd_msg_key(ev)


def _dedup_ok(account_id: str, group: str, msg_key: str) -> bool:
    if not msg_key:
        return True
    now = time.time()
    key = (account_id, group, msg_key)
    hit = _SEEN.get(key)
    if hit is not None and now - hit <= 3600:
        return False
    _SEEN[key] = now
    if len(_SEEN) > 5000:
        cutoff = now - 3600
        for k in [k for k, t in _SEEN.items() if t < cutoff]:
            _SEEN.pop(k, None)
    try:
        return pgs01_db.seen(account_id, group, msg_key, ttl_seconds=30 * 86400)
    except Exception as e:
        core.log(f"S01 dedup-ledger error :: {e!r}")
        return True


# --------------------------------------------------- album coalescing (additive)

def _album_enabled(rule: dict) -> bool:
    try:
        return int(rule.get("group_window_ms") or 0) > 0
    except Exception:
        return False


def _album_candidate(ev: dict, part: dict | None) -> bool:
    """True when this event is a bare image that may join a burst album.

    Only captionless images qualify: no typed body, no caption, not a reply,
    and no mentions. Anything richer (text+caption, @All, reply) is forwarded
    on its own so no meaning is folded away.
    """
    if not part or part.get("kind") not in ("image", "gif"):
        return False
    if isinstance(ev.get("quote"), dict):
        return False
    if (part.get("caption") or "").strip():
        return False
    if any(isinstance(m, dict) for m in (ev.get("mentions") or [])):
        return False
    return bool(part.get("url"))


def _album_key(rule: dict, gid: str, ev: dict) -> tuple:
    return (rule["id"], str(gid), str(ev.get("uidFrom") or ""))


def _album_push(rule: dict, ev: dict, part: dict, gid: str, msg_id: str, text: str) -> None:
    """Buffer a captionless image; the burst flushes as ONE album message."""
    delay = max(0.3, int(rule.get("group_window_ms") or 2000) / 1000.0)
    key = _album_key(rule, gid, ev)
    url = str(part.get("url") or "")
    now = time.time()
    with _ALBUM_LOCK:
        st = _ALBUM.get(key)
        if st is None:
            st = {
                "deadline": now + delay,
                "urls": [url],
                "item": {
                    "rule_id": rule["id"], "account_id": rule["account_id"],
                    "src_group_id": gid, "src_name": ev.get("name", ""),
                    "dst_group_id": rule["dst_group_id"], "src_msg_id": msg_id,
                    "src_msg_ids": [msg_id] if msg_id else [],
                    "kind": "plain",
                },
            }
            _ALBUM[key] = st
        else:
            if url and url not in st["urls"]:
                st["urls"].append(url)
            if msg_id:
                st["item"]["src_msg_ids"].append(msg_id)
            st["deadline"] = now + delay


def _album_flush(key: tuple) -> None:
    with _ALBUM_LOCK:
        st = _ALBUM.pop(key, None)
    if not st:
        return
    urls = st["urls"]
    if not urls:
        return
    item = st["item"]
    n = len(urls)
    item["parts"] = [{"kind": "image", "urls": list(urls), "caption": ""}]
    item["media"] = list(urls)
    item["text"] = ""
    item["log_text"] = f"[album {n} ảnh]"
    _enqueue(item)
    core.log(f"S01 album-flush :: {item['rule_id']} :: {item['src_group_id']} :: {n} ảnh")


def _album_flush_stale() -> None:
    now = time.time()
    with _ALBUM_LOCK:
        due = [k for k, st in _ALBUM.items() if st["deadline"] <= now]
    for k in due:
        _album_flush(k)


def _album_flush_sender(rule: dict, gid: str, ev: dict) -> None:
    """Flush a sender's pending album before their next non-image message.

    Preserves ordering: if a burst ends with a captioned image or text, the
    buffered captionless images must land FIRST rather than being deferred past
    the newer message.
    """
    _album_flush(_album_key(rule, gid, ev))


def _album_reaper() -> None:
    while True:
        time.sleep(0.5)
        try:
            _album_flush_stale()
        except Exception as e:  # pragma: no cover
            core.log(f"S01 album-reaper error :: {e!r}")


def start_album_reaper() -> None:
    global _ALBUM_REAPER_STARTED
    if _ALBUM_REAPER_STARTED:
        return
    _ALBUM_REAPER_STARTED = True
    threading.Thread(target=_album_reaper, daemon=True, name="s01-album").start()


# ------------------------------------------------------------- mention logic

def _mentions_all(mentions: list) -> bool:
    for m in mentions or []:
        if not isinstance(m, dict):
            continue
        if str(m.get("uid")) == "-1":
            return True
        try:
            if int(m.get("type") or 0) == 1:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _has_name_mention(mentions: list) -> bool:
    for m in mentions or []:
        if not isinstance(m, dict):
            continue
        uid = str(m.get("uid") or "")
        if uid and uid != "-1":
            return True
    return False


def _strip_mentions(text: str, mentions: list) -> str:
    """Remove every mention token span from the text (so a re-sent @All never
    duplicates the original token), then collapse whitespace."""
    if not text or not mentions:
        return (text or "").strip()
    spans: list[tuple[int, int]] = []
    for m in mentions:
        if not isinstance(m, dict):
            continue
        try:
            pos = int(m.get("pos"))
            ln = int(m.get("len"))
        except Exception:
            continue
        if pos >= 0 and ln > 0:
            spans.append((pos, pos + ln))
    if not spans:
        return text.strip()
    spans.sort()
    out: list[str] = []
    cur = 0
    for a, b in spans:
        if a > cur:
            out.append(text[cur:a])
        cur = max(cur, b)
    if cur < len(text):
        out.append(text[cur:])
    return re.sub(r"\s+", " ", "".join(out)).strip()


# --------------------------------------------------------------- reply logic

def _reply_context_line(quote: dict, rule: dict) -> str:
    """'↩ [dd/mm/YYYY ]hh:mm:ss: <quote text>' fallback context line."""
    msg = re.sub(r"\s+", " ", str(quote.get("msg") or "").strip())
    qline = (msg[:120] + "…") if len(msg) > 120 else (msg or "[nội dung khác]")
    tsline = ""
    if int(rule.get("reply_quote_ms") or 0) and quote.get("ts"):
        try:
            qt = time.localtime(float(quote["ts"]) / 1000.0)
            tsline = time.strftime("%H:%M:%S", qt)
            today = time.localtime()
            if (qt.tm_year, qt.tm_yday) != (today.tm_year, today.tm_yday):
                tsline = time.strftime("%d/%m/%Y ", qt) + tsline
        except Exception:
            tsline = ""
    return f"↩ {tsline}: {qline}" if tsline else f"↩ {qline}"


def _reply_by_search(rule: dict, ev: dict, gid: str) -> tuple[str, dict | None]:
    """Map a reply's parent in A to the forwarded copy in B.

    Primary key: the parent's msgId (quote.globalMsgId/msgId) looked up in our
    own forward ledger (src_msg_id -> dst_msg_id). Fallback: content search over
    the ledger. When a target msgId is found we return a native `quote` object
    (reply to that exact message in B); otherwise we return a context line.
    """
    rule_id = rule["id"]
    acc = rule.get("account_id")
    q = ev.get("quote") if isinstance(ev.get("quote"), dict) else {}
    parent_id = str(q.get("globalMsgId") or q.get("msgId") or "").strip()
    row = None
    if parent_id:
        try:
            row = pgs01_db.find_by_src(rule_id, gid, parent_id)
        except Exception:
            row = None
    if not row and q.get("msg"):
        try:
            row = pgs01_db.find_by_src_content(rule_id, gid, str(q.get("msg")))
        except Exception:
            row = None
    dst_msg = str((row or {}).get("dst_msg_id") or "").strip()
    if dst_msg:
        try:
            tsu = int(float(q["ts"]) / 1000.0)
        except Exception:
            tsu = int(time.time())
        content = q.get("msg")
        if not isinstance(content, str) or not content.strip():
            content = _reply_context_line(q, rule)
        qobj = {
            "uidFrom": str(q.get("ownerId") or "") or str(acc or ""),
            "msgId": dst_msg,
            "msgType": str(q.get("msgType") or "webchat"),
            "cliMsgId": str(q.get("cliMsgId") or ""),
            "propertyExt": {},
            "ts": tsu,
            "ttl": 0,
            "content": content or "",
        }
        return "", qobj
    return _reply_context_line(q, rule), None


# ------------------------------------------------------------------ worker

def _reader(account_id: str) -> None:
    """Attach to the shared session daemon and wire its events into the S01
    controller (mirrors core._bot_reader)."""
    bot = _BOTS.get(account_id)
    if not bot:
        return
    try:
        groups = core._bot_watch_groups(account_id)
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
                    core.log(f"S01 ready :: {account_id} (own {bot['own_id']})")
                    threading.Thread(target=_s01_apply_watch, args=(account_id,), daemon=True).start()
            elif kind == "msg":
                try:
                    _handle_message(account_id, ev)
                except Exception as e:
                    core.log(f"S01 msg error :: {e!r}")
            elif kind in ("listen-error", "listen-disconnected", "msg-error"):
                bot["error"] = str(ev.get("error") or ev.get("reason") or kind)[:300]
                bot["err_ts"] = time.time()
                core.log(f"S01 {kind} :: {account_id} :: {bot['error']}")
            elif kind == "listen-connected":
                bot["error"] = ""
                bot.pop("err_ts", None)
            elif kind == "heartbeat":
                _HB[account_id] = time.time()
                with _BOT_LOCK:
                    b2 = _BOTS.get(account_id)
                    if b2 is not None:
                        b2["hb"] = _HB[account_id]
            elif kind == "media-error":
                core.log(f"S01 media-error :: {account_id} :: {str(ev.get('error'))[:200]}")
            elif kind == "fatal":
                bot["error"] = str(ev.get("error") or "daemon fatal")[:300]
                bot["err_ts"] = time.time()
                core.log(f"S01 daemon-fatal :: {account_id} :: {bot['error']}")

        conn.subscribe(on_event)
        bot["conn"] = conn
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
                        core.log(f"S01 ready :: {account_id} (own {bot['own_id']}, attach)")
        except Exception:
            pass
    except Exception as e:  # pragma: no cover
        bot["error"] = str(e)[:300]
        core.log(f"S01 reader start error :: {account_id} :: {e!r}")


def _handle_message(account_id: str, ev: dict) -> None:
    _SEEN_ACTIVITY[account_id] = time.time()
    gid = str(ev.get("group") or "")
    if not gid:
        return  # S01 relays GROUP -> GROUP only
    text = (ev.get("text") or "").strip()
    msg_id = str(ev.get("msgId") or "")
    msg_key = _msg_key(ev)
    part = ev.get("part") if isinstance(ev.get("part"), dict) else None
    mentions = [m for m in (ev.get("mentions") or []) if isinstance(m, dict)]
    for rule in pgs01_db.list_rules():
        if not rule.get("enabled") or rule.get("account_id") != account_id:
            continue
        if str(rule.get("src_group_id")) != gid:
            continue
        if ev.get("isSelf") and not rule.get("include_self"):
            continue
        if not _dedup_ok(account_id, gid, msg_key):
            continue
        media = [u for u in (ev.get("media") or []) if isinstance(u, str) and u.startswith("http")]
        media_kind = str(ev.get("mediaKind") or "")
        if not part:
            if media:
                part = {"kind": media_kind or "image", "url": media[0], "caption": text}
            elif text:
                part = {"kind": "text", "text": text}
        if not text and not media and not part:
            pgs01_db.bump_rule(rule["id"], skipped=1)
            pgs01_db.add_log(rule["id"], gid, ev.get("name", ""), "(không phải tin chữ)",
                             "skip:rỗng", src_msg_id=msg_id)
            continue
        if msg_id:
            pgs01_db.bump_rule(rule["id"], last_seen_msg_id=msg_id)
        _dispatch(rule, ev, part, text, gid, msg_id, media, media_kind, mentions)


def _dispatch(rule: dict, ev: dict, part: dict | None, text: str, gid: str,
              msg_id: str, media: list, media_kind: str, mentions: list) -> None:
    """Route one source message: reply | @All | @Name(skip) | plain forward."""
    is_reply = isinstance(ev.get("quote"), dict)
    has_all = _mentions_all(mentions)
    has_name = _has_name_mention(mentions)
    base = {"rule_id": rule["id"], "account_id": rule["account_id"],
            "src_group_id": gid, "src_name": ev.get("name", ""),
            "dst_group_id": rule["dst_group_id"], "src_msg_id": msg_id,
            "parts": [part] if part else [], "text": text,
            "media": media, "media_kind": media_kind}
    # Ordering guard: anything that is NOT a coalescing candidate ends the
    # sender's pending burst, so buffered images land BEFORE the newer message.
    if _album_enabled(rule) and not (part and _album_candidate(ev, part)):
        _album_flush_sender(rule, gid, ev)
    if is_reply:
        if not rule.get("fw_replies", 1):
            pgs01_db.add_log(rule["id"], gid, ev.get("name", ""), text, "skip:reply-off",
                             src_msg_id=msg_id)
            pgs01_db.bump_rule(rule["id"], skipped=1)
            return
        rprefix, rquote = _reply_by_search(rule, ev, gid)
        if rprefix:
            base["reply_prefix"] = rprefix
        if rquote:
            base["quote"] = rquote
        base["kind"] = "reply"
        _enqueue(base)
        return
    if has_all:
        if not rule.get("fw_all", 1):
            pgs01_db.add_log(rule["id"], gid, ev.get("name", ""), text, "skip:all-off",
                             src_msg_id=msg_id)
            pgs01_db.bump_rule(rule["id"], skipped=1)
            return
        body = _strip_mentions(text, mentions)
        base["kind"] = "all"
        base["all_text"] = body
        _enqueue(base)
        return
    if has_name:
        if not rule.get("fw_name", 0):
            pgs01_db.add_log(rule["id"], gid, ev.get("name", ""), text, "skip:@Name",
                             src_msg_id=msg_id)
            pgs01_db.bump_rule(rule["id"], skipped=1)
            return
        # fw_name enabled: forward the mention-free text as a plain post
        base["text"] = _strip_mentions(text, mentions) or text
        base["kind"] = "plain"
        _enqueue(base)
        return
    # Additive album coalescing: a bare (captionless) image joins a burst from
    # the same sender and flushes as ONE multi-attachment message instead of
    # N separate ones. Falls through to the plain path when disabled.
    if _album_enabled(rule) and part and _album_candidate(ev, part):
        _album_push(rule, ev, part, gid, msg_id, text)
        pgs01_db.bump_rule(rule["id"], last_seen_msg_id=msg_id or "")
        return
    base["kind"] = "plain"
    _enqueue(base)


def _enqueue(item: dict) -> None:
    bot = _BOTS.get(item.get("account_id") or "")
    if not bot:
        pgs01_db.add_log(item["rule_id"], item.get("src_group_id", ""), item.get("src_name", ""),
                         item.get("text", ""), "skip: bot tắt", src_msg_id=item.get("src_msg_id", ""))
        pgs01_db.bump_rule(item["rule_id"], skipped=1)
        return
    bot["out"].put(item)


def _all_blocks(body: str, media: list) -> list:
    """Explicit bridge blocks for an @All forward: '@All <body>' with a real
    @All mention (uid=-1, type=1, pos=0, len=4), then any media."""
    blocks: list = [{"type": "text", "content": ("@All " + body).strip(),
                     "mentions": [{"pos": 0, "uid": "-1", "len": 4, "type": 1}]}]
    for u in media:
        blocks.append({"type": "image", "urls": [u], "caption": ""})
    return blocks


def _sender_loop(account_id: str) -> None:
    bot = _BOTS.get(account_id)
    if not bot:
        return
    q = bot["out"]
    while True:
        item = q.get()
        if item is None:
            break
        try:
            rule = pgs01_db.get_rule(item["rule_id"])
            if not rule or not rule.get("enabled"):
                continue
            if core.killswitch_active():
                pgs01_db.add_log(rule["id"], item["src_group_id"], item["src_name"],
                                 item["text"], "skip: killswitch", src_msg_id=item.get("src_msg_id", ""))
                pgs01_db.bump_rule(rule["id"], skipped=1)
                continue
            now = time.time()
            if pgs01_db.count_sent_since(rule["id"], int(now) - 3600) >= int(rule.get("max_per_hour") or 60):
                pgs01_db.add_log(rule["id"], item["src_group_id"], item["src_name"],
                                 item["text"], "skip: vượt hạn/giờ", src_msg_id=item.get("src_msg_id", ""))
                pgs01_db.bump_rule(rule["id"], skipped=1, error="vượt hạn/giờ")
                continue
            gap = max(5, int(rule.get("gap_seconds") or 0))
            wait = gap - (now - _LAST.get(rule["id"], 0))
            if wait > 0:
                time.sleep(min(wait, gap))
            dry = bool(rule.get("dry_run"))
            prefix = rule.get("prefix") or ""
            kind = item.get("kind") or "plain"
            if kind == "all":
                cmd = {"cmd": "send", "thread": str(rule["dst_group_id"]), "type": "group",
                       "dryRun": dry,
                       "blocks": _all_blocks(item.get("all_text", ""), item.get("media") or [])}
                if prefix:
                    cmd["blocks"][0]["content"] = prefix + cmd["blocks"][0]["content"]
            else:
                cmd = core._fwd_send_cmd(str(rule["dst_group_id"]), item.get("text") or "",
                                         item.get("parts") or [], prefix, dry,
                                         reply_prefix=item.get("reply_prefix") or "",
                                         quote=item.get("quote") or None)
            res = core._bot_cmd(account_id, cmd, timeout=60)
            _LAST[rule["id"]] = time.time()
            ok = bool(res.get("ok"))
            status = "dry" if (ok and dry) else ("sent" if ok else "error")
            log_text = item.get("log_text") or item.get("text") or (
                f"[{len(res.get('msgIds') or [])} phần]" if item.get("parts") else "")
            err = "" if ok else str(res.get("error") or "lỗi gửi")
            # Additive: an album carries several source msgIds but ONE dst
            # message. Log a row per source id (same dst_msg_id) so the
            # source->destination trail stays 1:1 for replies/audit.
            src_ids = item.get("src_msg_ids") or ([item.get("src_msg_id")] if item.get("src_msg_id") else [""])
            written = False
            for sid in src_ids:
                w = pgs01_db.add_log(rule["id"], item["src_group_id"], item["src_name"], log_text,
                                     status, res.get("msgId", ""), src_msg_id=sid, error=err)
                written = written or w
            if not written:
                core.log(f"S01 dup-db :: {rule['id']} :: {item['src_group_id']} "
                         f"msg={item.get('src_msg_id', '')}")
                continue
            pgs01_db.bump_rule(rule["id"], forwarded=(1 if (ok and not dry) else 0),
                               skipped=(0 if ok else 1),
                               error=("" if ok else str(res.get("error") or "lỗi gửi")))
            core.log(f"S01 {status} :: {rule['id']} :: {item['src_group_id']} -> "
                     f"{rule['dst_group_id']} :: [{kind}] {(log_text or '')[:40]}")
        except Exception as e:  # pragma: no cover
            core.log(f"S01 sender error :: {e!r}")


# --------------------------------------------------------------- lifecycle

def start_bot(account_id: str) -> dict:
    account_id = (account_id or "").strip()
    if not core.role_allows_s01():
        return {"ok": False, "error": "tiến trình này không chạy S01 (ZS_ROLE)"}
    if not account_id:
        return {"ok": False, "error": "thiếu account"}
    if not core.account_enabled(account_id):
        return {"ok": False, "error": "nick đang tắt trong panel"}
    if not _creds_ok(account_id):
        return {"ok": False, "error": "nick chưa đăng nhập (thiếu credentials)"}
    with _BOT_LOCK:
        b = _BOTS.get(account_id)
        if b and b.get("conn") is not None and b["conn"].ready:
            return {"ok": True, "already": True, "account": account_id}
        groups = _s01_watch_groups(account_id)
        bot = {"account": account_id, "started": time.time(),
               "ready": threading.Event(), "error": "", "own_id": "",
               "out": queue.Queue(), "conn": None,
               "watch": {"mode": ("only" if groups else "all"), "count": len(groups)}}
        _BOTS[account_id] = bot
        threading.Thread(target=_reader, args=(account_id,), daemon=True).start()
        threading.Thread(target=_sender_loop, args=(account_id,), daemon=True).start()
    ok = bot["ready"].wait(40)
    db.log_event("s01_start", f"{account_id}", account_id=account_id)
    core.log(f"S01 start :: {account_id}{'' if ok else ' (chưa ready)'} "
             f"[watch {bot.get('watch', {}).get('mode')} {bot.get('watch', {}).get('count')}]")
    return {"ok": True, "account": account_id, "ready": ok, "watch": bot.get("watch")}


def stop_bot(account_id: str) -> dict:
    account_id = (account_id or "").strip()
    with _BOT_LOCK:
        b = _BOTS.pop(account_id, None)
    if not b:
        return {"ok": True, "already": True}
    try:
        b["out"].put(None)
    except Exception:
        pass
    conn = b.get("conn")
    if conn is not None:
        try:
            conn._subs.clear()
        except Exception:
            pass
    db.log_event("s01_stop", f"{account_id}", account_id=account_id)
    core.log(f"S01 stop :: {account_id}")
    return {"ok": True, "account": account_id}


def restart_bot(account_id: str) -> dict:
    stop_bot(account_id)
    return start_bot(account_id)


def bot_status() -> list[dict]:
    out = []
    with _BOT_LOCK:
        for aid, b in _BOTS.items():
            conn = b.get("conn")
            alive = bool(conn is not None and conn.ready)
            hb = _HB.get(aid, 0)
            out.append({
                "account_id": aid, "running": alive, "ready": b["ready"].is_set(),
                "own_id": b.get("own_id", ""), "started": b.get("started"),
                "last_msg": _SEEN_ACTIVITY.get(aid), "error": b.get("error", ""),
                "queue": b["out"].qsize(),
                "watch": b.get("watch") or {"mode": "all", "count": 0},
                "socket_alive": hb > 0 and (time.time() - hb) < 180,
                "last_hb": hb,
            })
    return out


def snapshot() -> dict:
    rules = pgs01_db.list_rules()
    bots = {b["account_id"]: b for b in bot_status()}
    for r in rules:
        r["bot"] = bots.get(r["account_id"]) or {"running": False, "ready": False,
                                                 "account_id": r["account_id"]}
        r["sent_last_hour"] = pgs01_db.count_sent_since(r["id"], int(time.time()) - 3600)
    return {"rules": rules, "bots": list(bots.values()), "kill": core.killswitch_active()}


def ensure_bots() -> dict:
    if not core.role_allows_s01():
        return {"ok": True, "started": [], "skipped": "role"}
    started = []
    accts = {r["account_id"] for r in pgs01_db.list_rules()
             if r.get("enabled") and r.get("account_id")}
    for aid in accts:
        with _BOT_LOCK:
            b = _BOTS.get(aid)
            running = bool(b and b.get("conn") is not None and b["conn"].ready)
        if not running:
            r = start_bot(aid)
            if r.get("ok"):
                started.append(aid)
    return {"ok": True, "started": started}


def _health_monitor() -> None:
    QUIET_S = 6 * 3600
    HB_STALE_S = 180
    TICK = 60
    while True:
        try:
            accts = {r["account_id"] for r in pgs01_db.list_rules()
                     if r.get("enabled") and r.get("account_id")}
            now = time.time()
            for aid in accts:
                with _BOT_LOCK:
                    b = _BOTS.get(aid)
                    conn = b.get("conn") if b else None
                    alive = bool(conn is not None and conn.ready)
                    queued = b["out"].qsize() if b else 0
                    started = (b.get("started") or now) if b else now
                uptime = now - started
                last_msg = _SEEN_ACTIVITY.get(aid) or 0
                quiet = (now - last_msg) if last_msg else 0
                hb = _HB.get(aid) or 0
                stale_hb = (hb == 0) or (now - hb > HB_STALE_S)
                if not alive:
                    core.log(f"S01 health :: {aid} worker chết → restart")
                    restart_bot(aid)
                elif stale_hb and uptime > 120:
                    core.log(f"S01 health :: {aid} socket im → restart")
                    restart_bot(aid)
                elif quiet > QUIET_S and uptime > QUIET_S and queued == 0:
                    core.log(f"S01 health :: {aid} im nguồn {int(quiet)}s → refresh listener")
                    restart_bot(aid)
        except Exception as e:  # never die
            core.log(f"S01 health error :: {e!r}")
        time.sleep(TICK)


def start_health_monitor() -> None:
    global _MONITOR_STARTED
    if _MONITOR_STARTED:
        return
    if not core.role_allows_s01():
        return
    _MONITOR_STARTED = True
    start_album_reaper()
    threading.Thread(target=_health_monitor, daemon=True, name="s01-health").start()
