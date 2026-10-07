#!/usr/bin/env python3
"""zaloclaw-studio — control panel (FastAPI + HTMX).

Local/Tailscale only. Operator surface for Zalo marketing: QR login, contacts,
bulk message/friend campaigns with rate-limit + dry-run + killswitch.
"""
from __future__ import annotations

import csv
import faulthandler
import io
import json
import re
import signal
import sys
import time
import uuid
import unicodedata
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import core  # noqa: E402
import db  # noqa: E402

db.init()

# Diagnostic: `kill -USR1 <pid>` dumps every thread's Python stack into the log
# file (SIGUSR1 -> py-spy-free introspection of a wedged worker thread).
try:
    faulthandler.register(signal.SIGUSR1, all_threads=True)
except Exception:
    pass

app = FastAPI(title="zaloclaw-studio", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _autobot_boot() -> None:
    """Re-arm listener bots for any enabled forward rules after a restart."""
    try:
        res = core.ensure_bots()
        if res.get("started"):
            core.log(f"AUTOBOT boot :: started {res['started']}")
    except Exception as e:  # never block startup
        core.log(f"AUTOBOT boot error :: {e!r}")
    try:
        core.start_health_monitor()
    except Exception as e:  # never block startup
        core.log(f"AUTOBOT health-monitor boot error :: {e!r}")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Defence-in-depth headers + no-store on HTML/API/UI responses. The panel is
    local/Tailscale only, but these also blunt a stray cross-site form POST.

    NOTE: the ROOT page '/' carries the app's inline JS (all button handlers),
    so it MUST be no-store too — otherwise a browser can serve a stale page with
    old/broken JS after a fix. Only static assets keep their ETag caching.
    """
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    p = request.url.path
    is_html = resp.headers.get("content-type", "").startswith("text/html")
    if is_html or p.startswith(("/ui/", "/files/", "/contacts/", "/campaigns/", "/groups/")):
        resp.headers["Cache-Control"] = "no-store"
    return resp
templates = Jinja2Templates(directory=str(HERE / "templates"))
static_dir = HERE / "static"
static_dir.mkdir(exist_ok=True)

MAX_UPLOAD = 20 * 1024 * 1024  # 20 MB hard cap for CSV uploads (local app)
MAX_IMAGE = 25 * 1024 * 1024   # 25 MB per image
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MEDIA_DIR = HERE.parent / "data" / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _safe_name(name: str) -> str:
    stem = Path(name).stem[:40]
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem) or "img"
    ext = Path(name).suffix.lower()
    if ext not in IMAGE_EXTS:
        ext = ".jpg"
    return f"{stem}_{uuid.uuid4().hex[:8]}{ext}"

app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

templates.env.filters["datetime_hm"] = lambda ts: (
    time.strftime("%H:%M:%S %d/%m", time.localtime(int(ts))) if ts else "—")
templates.env.filters["ts_fmt"] = lambda ts: (
    time.strftime("%H:%M:%S %d/%m", time.localtime(int(ts))) if ts else "—")
templates.env.filters["fromjson"] = lambda s: (json.loads(s) if s else [])


def _media_url(p: str) -> str:
    """Public /media URL for an absolute stored image path ('' when missing)."""
    try:
        fp = Path(p)
        if fp.exists() and fp.parent == MEDIA_DIR:
            return f"/media/{fp.name}"
    except Exception:
        pass
    return ""


templates.env.filters["media_url"] = _media_url


def _parse_tags(raw) -> list[str]:
    """Accept comma/semicolon-separated tags, a repeated form field (list), or
    ';'-joined values; trim, drop blanks, de-dupe preserving order."""
    parts: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            parts.extend(str(item).replace(";", ",").split(","))
    else:
        parts = str(raw or "").replace(";", ",").split(",")
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


# A phone (8–20 digits, optional leading +) or a long Zalo user id. Used to pull
# real targets out of freeform pasted text.
_PHONE_TOKEN_RE = re.compile(r"\+?\d{8,20}")


def _parse_phones(raw) -> list[str]:
    """Extract phone numbers / Zalo user ids from freeform pasted text.

    Accepts values separated by newlines, commas, semicolons, tabs or spaces,
    and also 'phone, name, gender' rows (only the number survives). De-dupes
    while preserving order.

    NOTE: the old code did `text.splitlines()` + `line.split(",")[0]`, which
    silently DROPPED every target after the first comma on a line — pasting
    '0969137218,0919943776' kept only the first phone. This fixes that.
    """
    if not raw:
        return []
    out: list[str] = []
    for m in _PHONE_TOKEN_RE.findall(str(raw)):
        # canonicalise so '+84912...' and '0912...' collapse to one target (F3)
        c = core.norm_phone(m) or m
        if c not in out:
            out.append(c)
    return out


async def _save_campaign_uploads(request: Request) -> list[str]:
    """Persist any uploaded images (form field 'images', multiple) to
    data/media and return their absolute paths. Silently skips non-images and
    oversize files; raises HTTPException only on a hard read error."""
    try:
        form = await request.form()
    except Exception:
        return []
    saved: list[str] = []
    for field in form.getlist("images"):
        fn = getattr(field, "filename", None)
        if not fn:
            continue
        ext = Path(fn).suffix.lower()
        if ext not in IMAGE_EXTS:
            continue
        data = await field.read()
        if not data:
            continue
        if len(data) > MAX_IMAGE:
            raise HTTPException(status_code=413, detail=f"Ảnh {fn} vượt {MAX_IMAGE // (1024*1024)}MB")
        dest = MEDIA_DIR / _safe_name(fn)
        dest.write_bytes(data)
        saved.append(str(dest))
    return saved


def _media_rel(p: str) -> str:
    """Public /media/<file> URL for a stored absolute image path ('' if unknown)."""
    try:
        fp = Path(p).resolve()
        if fp.exists() and fp.parent == MEDIA_DIR:
            return f"/media/{fp.name}"
    except Exception:
        pass
    return ""


async def _campaign_blocks(request: Request, prev_json: str = "") -> str:
    """Build the ordered block list from a submitted form and return its JSON.

    Preferred source is `blocks_json`: a JSON array whose image blocks may use
    `media_ref` (a /media/<file> URL for an already-saved photo) or `src`
    (absolute stored path). Any freshly uploaded files in the multipart field
    `up_N` (block index N) are saved to data/media and given back as `src`.

    When `blocks_json` is absent (legacy forms), falls back to body_template +
    url + the multi-image field `images` so nothing breaks.
    """
    try:
        form = await request.form()
    except Exception:
        form = None

    raw = form.get("blocks_json") if form is not None else None
    if raw is None or str(raw).strip() == "":
        # Legacy path: only when this submission carries NO block field at all.
        # An explicitly-empty blocks_json is handled below (empties the body).
        if raw is not None:
            return core.blocks_to_json([])
        body = core.normalize_text(form.get("body_template") or "") if form is not None else ""
        url = (form.get("url") or "").strip() if form is not None else ""
        imgs = await _save_campaign_uploads(request)
        blocks: list[dict] = []
        if body:
            blocks.append({"type": "text", "content": body})
        for p in imgs:
            blocks.append({"type": "image", "src": p, "caption": ""})
        if url:
            blocks.append({"type": "link", "url": url, "caption": ""})
        return core.blocks_to_json(blocks)

    # New path: resolve each block, persisting any newly uploaded photos.
    try:
        arr = json.loads(raw)
        if not isinstance(arr, list):
            raise ValueError("blocks_json must be a JSON array")
    except Exception:
        # Malformed JSON = a real client bug. Fail loudly instead of silently
        # wiping the campaign body (and never fall back to clearing content).
        raise HTTPException(status_code=400, detail="blocks_json không phải JSON hợp lệ")

    out: list[dict] = []
    for i, b in enumerate(arr if isinstance(arr, list) else []):
        if not isinstance(b, dict):
            continue
        t = str(b.get("type") or "").lower()
        if t == "image":
            src = str(b.get("src") or "").strip()
            ref = str(b.get("media_ref") or "").strip()
            if ref.startswith("/media/"):
                cand = MEDIA_DIR / ref[len("/media/"):]
                if cand.exists() and cand.parent == MEDIA_DIR:
                    src = str(cand)
            up = form.get(f"up_{i}") if form is not None else None
            if up is not None and getattr(up, "filename", None):
                fn = up.filename
                if Path(fn).suffix.lower() in IMAGE_EXTS:
                    data = await up.read()
                    if data and len(data) <= MAX_IMAGE:
                        dest = MEDIA_DIR / _safe_name(fn)
                        dest.write_bytes(data)
                        src = str(dest)
            if src and Path(src).exists() and Path(src).parent == MEDIA_DIR:
                out.append({"type": "image", "src": src,
                            "caption": core.normalize_text(b.get("caption") or "")})
        elif t == "text":
            content = core.normalize_text(b.get("content") or "")
            if content:
                out.append({"type": "text", "content": content})
        elif t == "link":
            u = str(b.get("url") or "").strip()
            if u:
                out.append({"type": "link", "url": u,
                            "caption": core.normalize_text(b.get("caption") or "")})
    return core.blocks_to_json(out)


def _is_dry(v: str) -> bool:
    """True when a campaign/pipeline should be DRY-RUN. The form sends a hidden
    'dry_run=off' plus a 'dry_run' checkbox (checked = DRY). htmx may join
    duplicate keys, so take the LAST value. Empty/absent -> default DRY (safe)."""
    parts = [p.strip().lower() for p in str(v).replace(";", ",").split(",") if p.strip()]
    return (not parts) or parts[-1] in ("on", "1", "true", "yes", "dry")


def _last_bool(v: str, default: bool = False) -> bool:
    """Boolean from a (possibly htmx-joined) form field. A hidden '0' + a
    checkbox '1' arrive as '0,1' (on) or '0' (off): take the LAST value."""
    parts = [p.strip().lower() for p in str(v).replace(";", ",").split(",") if p.strip()]
    if not parts:
        return default
    return parts[-1] in ("1", "on", "true", "yes")


def _reply_mode(v: str) -> str:
    """Validate the reply-handling mode for a pipeline rule."""
    m = str(v or "").strip().lower()
    return m if m in ("text", "quote", "skip") else "text"


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    """Return a short, human-readable JSON instead of FastAPI's 422 dump, so the
    UI toast can show why a button did nothing (usually a required field empty)."""
    fields = OrderedDict()
    for e in exc.errors():
        loc = [str(x) for x in e.get("loc", []) if x not in ("body", "query", "path")]
        name = ".".join(loc) or "form"
        fields[name] = e.get("msg", "invalid")
    return JSONResponse(
        {"ok": False, "error": "Dữ liệu chưa hợp lệ: " + "; ".join(f"{k} ({v})" for k, v in fields.items()),
         "fields": fields}, status_code=422)


def _ctx(request: Request, **extra) -> dict:
    accounts = db.list_accounts()
    return {
        "request": request,
        "accounts": accounts,
        "role": core.ROLE,
        "acct_facts": {a["id"]: core.account_facts(a["id"]) for a in accounts},
        "campaigns": db.list_campaigns(50),
        "contacts": db.list_contacts(300),
        "files": db.list_contact_files(),
        "groups": db.list_groups(limit=500),
        "scan_status": {a["id"]: core.group_scan_status(a["id"]) for a in accounts},
        "scans": db.scan_progress_recent(50),
        "scan_map": {p["group_id"]: p for p in db.scan_progress_recent(500)},
        "defaults": (core.settings().get("defaults") or {}),
        "kill": core.killswitch_active(),
        "quiet": core.quiet_status(),
        "qdepth": core.queue_depth(),
        "settings": core.settings(),
        "all_tags": db.all_tags(),
        **extra,
    }


# --------------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(request))


# ----------------------------------------------------------------- fragments

@app.get("/ui/tab/{name}", response_class=HTMLResponse)
def ui_tab(request: Request, name: str):
    allowed = {"accounts", "contacts", "groups", "campaigns", "autobot", "logs", "scans", "settings"}
    if name not in allowed:
        name = "accounts"
    return templates.TemplateResponse(request, f"_tab_{name}.html", _ctx(request))


@app.get("/ui/accounts", response_class=HTMLResponse)
def ui_accounts(request: Request):
    return templates.TemplateResponse(request, "_accounts.html", _ctx(request))


# ------------------------------------------------------------------ filters
# Extra filters used by the 5s-refreshed campaign partial (fromjson/media_url
# already registered above; only add the block-summary helper here).
if "blk_summary" not in templates.env.filters:
    templates.env.filters["blk_summary"] = lambda c: core.blocks_summary(core.parse_blocks(c))
if "blk_json" not in templates.env.filters:
    templates.env.filters["blk_json"] = lambda c: core.blocks_to_json(core.parse_blocks(c))


@app.get("/ui/campaigns", response_class=HTMLResponse)
def ui_campaigns(request: Request):
    # Lightweight context: the campaigns tab refreshes every 5s, so avoid the
    # full _ctx() (which also loads contacts/groups/scan maps).
    return templates.TemplateResponse(request, "_campaigns.html", {
        "request": request,
        "campaigns": db.list_campaigns(50),
        "accounts": db.list_accounts(),
        "files": db.list_contact_files(),
        "all_tags": db.all_tags(),
    })


@app.get("/ui/contacts", response_class=HTMLResponse)
def ui_contacts(request: Request):
    return templates.TemplateResponse(request, "_contacts.html", _ctx(request))


@app.get("/ui/groups", response_class=HTMLResponse)
def ui_groups(request: Request):
    return templates.TemplateResponse(request, "_groups.html", _ctx(request))


@app.get("/ui/group/{gid}", response_class=HTMLResponse)
def ui_group_detail(request: Request, gid: str, account_id: str = ""):
    gname = next((g["name"] for g in db.list_groups(limit=2000) if g["group_id"] == gid), gid)
    accounts = db.list_accounts()
    aid = (account_id or "").strip()
    if not aid and accounts:
        aid = accounts[0]["id"]
    return templates.TemplateResponse(request, "_group_detail.html", {
        "gid": gid,
        "gname": gname,
        "members": db.list_members(gid, 2000),
        "account_id": aid,
        "default_file_name": f"{gname} - {time.strftime('%Y-%m-%d %H-%M')}",
    })


@app.get("/ui/logs", response_class=HTMLResponse)
def ui_logs(request: Request):
    return templates.TemplateResponse(request, "_logs.html",
                                      {"logs": core.logs(200), "qdepth": core.queue_depth()})


@app.get("/ui/campaign/{cid}", response_class=HTMLResponse)
def ui_campaign_detail(request: Request, cid: str):
    camp = db.get_campaign(cid)
    return templates.TemplateResponse(request, "_campaign_detail.html", {
        "camp": camp,
        "counts": db.campaign_counts(cid) if camp else {},
        "targets": db.targets_for(cid, 200) if camp else [],
    })


@app.get("/ui/campaign/{cid}/counts", response_class=HTMLResponse)
def ui_campaign_counts(cid: str):
    c = db.campaign_counts(cid) or {}
    return HTMLResponse(
        f'<span class="pill sent">✓ {c.get("sent", 0)}</span> '
        f'<span class="pill pending">{c.get("pending", 0)}</span> '
        f'<span class="pill failed">{c.get("failed", 0)}</span>'
    )


# ------------------------------------------------------------------ accounts

@app.post("/accounts/add")
def accounts_add(account_id: str = Form(""), name: str = Form("")):
    """Legacy/compat entry: create an account slot AND start QR login so a nick
    is never left empty. Empty account_id auto-picks the next 'nickN'."""
    aid = account_id.strip()
    if not aid:
        existing = {a["id"] for a in db.list_accounts()}
        i = 1
        while f"nick{i}" in existing:
            i += 1
        aid = f"nick{i}"
    db.upsert_account(aid, name=name.strip() or aid, status="pending_qr")
    db.log_event("account_add", aid, account_id=aid)
    core.start_qr_login(aid)
    return JSONResponse({"ok": True, "account_id": aid})


@app.post("/accounts/new")
def accounts_new():
    """Create a new Zalo account slot with an auto-generated id and start QR login."""
    existing = {a["id"] for a in db.list_accounts()}
    i = 1
    while f"nick{i}" in existing:
        i += 1
    aid = f"nick{i}"
    db.upsert_account(aid, name=f"Zalo {i}", status="pending_qr")
    db.log_event("account_add", aid, account_id=aid)
    core.start_qr_login(aid)
    return JSONResponse({"ok": True, "account_id": aid})


@app.post("/accounts/{aid}/qr")
def accounts_qr(aid: str):
    core.start_qr_login(aid)
    return JSONResponse({"ok": True, "account_id": aid})


@app.get("/ui/accounts/{aid}/qr", response_class=HTMLResponse)
def ui_account_qr(request: Request, aid: str):
    st = core.qr_status(aid)
    last = st.get("last") or {}
    # Auto-refresh while pending and not logged in
    done = st["logged_in"] or last.get("event") in ("done", "error")
    if st["logged_in"]:
        core.refresh_account_info(aid)
    return templates.TemplateResponse(request, "_qr.html", {
        "aid": aid, "st": st, "last": last, "done": done,
    })


@app.post("/accounts/{aid}/refresh")
def accounts_refresh(aid: str):
    return JSONResponse(core.refresh_account_info(aid))


@app.post("/accounts/{aid}/check")
def accounts_check(aid: str):
    """Live session probe (whoami). Marks the nick 'expired' if the session died."""
    return JSONResponse(core.account_session_ok(aid))


@app.post("/accounts/{aid}/rename")
def accounts_rename(aid: str, name: str = Form(...)):
    return JSONResponse(core.rename_account(aid, name))


@app.post("/accounts/{aid}/toggle")
def accounts_toggle(aid: str, enabled: str = Form("1")):
    return JSONResponse(core.set_account_enabled(aid, str(enabled).strip() in ("1", "on", "true")))


@app.post("/accounts/{aid}/delete")
def accounts_delete(aid: str, remove_creds: str = Form("1")):
    return JSONResponse(core.delete_account(aid, remove_creds=str(remove_creds).strip() in ("1", "on", "true")))


# ------------------------------------------------------------------ contacts

@app.post("/contacts/import")
def contacts_import(text: str = Form(""), tags: str = Form("")):
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("phone"):
            continue
        parts = [p.strip() for p in line.replace("\t", ",").split(",")]
        phone = parts[0]
        name = parts[1] if len(parts) > 1 else ""
        gender = parts[2] if len(parts) > 2 else ""
        if phone:
            rows.append({"phone": phone, "display_name": name, "gender": gender, "tags": tags})
    n = db.add_contacts(rows)
    db.log_event("contacts_import", f"{n} rows")
    return JSONResponse({"ok": True, "imported": n})


# ------------------------------------------------------------- contact files

@app.get("/ui/contact-files", response_class=HTMLResponse)
def ui_contact_files(request: Request):
    return templates.TemplateResponse(request, "_contact_files.html",
                                      {"files": db.list_contact_files()})


@app.get("/ui/file/{fid}", response_class=HTMLResponse)
def ui_file_detail(request: Request, fid: int):
    return templates.TemplateResponse(request, "_file_detail.html", {
        "fid": fid,
        "file": db.get_contact_file(fid),
        "members": db.list_file_members(fid),
        "accounts": db.list_accounts(),
        "groups": db.list_groups(limit=500),
    })


def _parse_lines(text: str) -> list[dict]:
    rows = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("phone"):
            continue
        parts = [p.strip() for p in line.replace("\t", ",").replace(";", ",").split(",")]
        phone = parts[0]
        name = parts[1] if len(parts) > 1 else ""
        gender = parts[2] if len(parts) > 2 else ""
        if phone:
            rows.append({"phone": phone, "display_name": name, "gender": gender})
    return rows


@app.post("/files/new")
def files_new(name: str = Form("Tệp KH"), source: str = Form("manual"), text: str = Form("")):
    fid = db.create_contact_file(name, source=source)
    n = db.add_file_members(fid, _parse_lines(text)) if text.strip() else 0
    db.log_event("file_new", f"#{fid} {name} ({n})")
    return JSONResponse({"ok": True, "file_id": fid, "added": n})


@app.post("/files/new-csv")
async def files_new_csv(name: str = Form("Tệp KH"), file: UploadFile = File(...)):
    raw = await file.read(MAX_UPLOAD + 1)
    rows = _parse_csv_bytes(raw)
    fid = db.create_contact_file(name.strip() or "Tệp KH", source=f"csv:{file.filename}")
    n = db.add_file_members(fid, rows)
    db.log_event("file_new_csv", f"#{fid} {file.filename}: +{n}/{len(rows)}")
    return JSONResponse({"ok": True, "file_id": fid, "added": n, "parsed": len(rows)})


@app.post("/files/{fid}/import")
def files_import(fid: int, text: str = Form("")):
    n = db.add_file_members(fid, _parse_lines(text))
    db.log_event("file_import", f"#{fid}: +{n}")
    return JSONResponse({"ok": True, "added": n})


def _parse_csv_bytes(raw: bytes) -> list[dict]:
    """Parse an uploaded CSV (header optional). Recognises columns by name
    (phone/so_dien_thoai/sdt, name/ten, gender/gioi_tinh) else positional."""
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail=f"File quá lớn (>{MAX_UPLOAD // (1024*1024)}MB)")
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except Exception:
        dialect = csv.excel
    reader = list(csv.reader(io.StringIO(text), dialect))
    if not reader:
        return []
    header = [h.strip().lower() for h in reader[0]]
    phone_keys = {"phone", "sdt", "so_dien_thoai", "sđt", "số điện thoại", "dien thoai", "điện thoại", "tel", "mobile"}
    name_keys = {"name", "ten", "tên", "ho_ten", "họ tên", "fullname", "display_name"}
    gender_keys = {"gender", "gioi_tinh", "giới tính", "sex", "phai", "phái"}
    has_header = any(h in phone_keys for h in header)
    idx = {"phone": 0, "name": 1, "gender": 2}
    if has_header:
        for i, h in enumerate(header):
            if h in phone_keys:
                idx["phone"] = i
            elif h in name_keys:
                idx["name"] = i
            elif h in gender_keys:
                idx["gender"] = i
    rows = reader[1:] if has_header else reader
    out = []
    for r in rows:
        if not r or not any(str(x).strip() for x in r):
            continue

        def g(k):
            i = idx[k]
            return str(r[i]).strip() if i < len(r) else ""
        phone = g("phone")
        if phone:
            out.append({"phone": phone, "display_name": g("name"), "gender": g("gender")})
    return out


@app.post("/files/{fid}/upload")
async def files_upload(fid: int, file: UploadFile = File(...)):
    raw = await file.read(MAX_UPLOAD + 1)
    rows = _parse_csv_bytes(raw)
    n = db.add_file_members(fid, rows)
    db.log_event("file_upload", f"#{fid} {file.filename}: +{n}/{len(rows)}")
    return JSONResponse({"ok": True, "added": n, "parsed": len(rows), "filename": file.filename})


@app.get("/files/{fid}/export.csv")
def files_export(fid: int):
    f = db.get_contact_file(fid) or {}
    members = db.list_file_members(fid, 100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["phone", "name", "gender", "zalo_user_id"])
    for m in members:
        w.writerow([m.get("phone", ""), m.get("display_name", ""), m.get("gender", ""),
                    m.get("zalo_user_id") or ""])
    data = "\ufeff" + buf.getvalue()  # BOM so Excel reads UTF-8
    raw_name = f.get("name") or f"file{fid}"
    # HTTP headers are latin-1; a Vietnamese file name ("Forex cộng đồng…") made
    # Starlette's header encode raise UnicodeEncodeError -> HTTP 500. Fold to
    # ASCII (drop diacritics) so any name yields a valid header.
    ascii_name = unicodedata.normalize("NFKD", raw_name).encode("ascii", "ignore").decode("ascii")
    safe = "".join(ch for ch in ascii_name if ch.isalnum() or ch in " -_").strip() or f"file{fid}"
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{safe}.csv"'})


@app.get("/contacts/export.csv")
def contacts_export_all():
    rows = db.list_contacts(100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["phone", "name", "gender", "zalo_user_id"])
    for m in rows:
        w.writerow([m.get("phone", ""), m.get("display_name", ""), m.get("gender", ""),
                    m.get("zalo_user_id") or ""])
    data = "\ufeff" + buf.getvalue()
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="contacts.csv"'})


@app.post("/files/{fid}/from-group")
def files_from_group(fid: int, gid: str = Form(...), account_id: str = Form("default")):
    if not db.get_contact_file(fid):
        return JSONResponse({"ok": False, "error": "tệp không tồn tại"})
    members = db.list_members(gid, 5000)
    if not members:
        return JSONResponse({"ok": False, "error": "nhóm chưa có thành viên đã lấy — hãy quét nhóm trước"})
    rows = [{"phone": m["user_id"], "zalo_user_id": m["user_id"], "display_name": m["display_name"]}
            for m in members]
    n = db.add_file_members(fid, rows)
    db.log_event("file_from_group", f"#{fid} <- group {gid}: +{n}", account_id=account_id)
    return JSONResponse({"ok": True, "added": n, "total_members": len(members)})


@app.post("/files/new-from-group")
def files_new_from_group(gid: str = Form(...), name: str = Form("")):
    gname = next((g["name"] for g in db.list_groups(limit=2000) if g["group_id"] == gid), gid)
    fname = (name or "").strip() or f"{gname} - {time.strftime('%Y-%m-%d %H-%M')}"
    members = db.list_members(gid, 5000)
    fid = db.create_contact_file(fname, source=f"group:{gid}")
    rows = [{"phone": m["user_id"], "zalo_user_id": m["user_id"], "display_name": m["display_name"]}
            for m in members]
    n = db.add_file_members(fid, rows)
    db.log_event("file_from_group", f"#{fid} {fname} <- group {gid}: +{n}")
    return JSONResponse({"ok": True, "file_id": fid, "name": fname, "added": n})


@app.post("/files/{fid}/rename")
def files_rename(fid: int, name: str = Form(...)):
    db.rename_contact_file(fid, name)
    return JSONResponse({"ok": True})


@app.post("/files/{fid}/delete")
def files_delete(fid: int):
    db.delete_contact_file(fid)
    db.log_event("file_delete", f"#{fid}")
    return JSONResponse({"ok": True})


@app.get("/ui/file-members/{fid}", response_class=HTMLResponse)
def ui_file_members(request: Request, fid: int):
    return templates.TemplateResponse(request, "_file_members.html",
                                      {"fid": fid, "members": db.list_file_members(fid)})


@app.post("/files/{fid}/member/remove")
def files_member_remove(fid: int, phone: str = Form(...)):
    db.remove_file_member(fid, phone.strip())
    return JSONResponse({"ok": True})


@app.post("/contacts/update")
def contacts_update(phone: str = Form(...), new_phone: str = Form(""),
                    display_name: str = Form(""), gender: str = Form("")):
    db.update_contact(phone.strip(), new_phone=new_phone or None,
                      display_name=display_name, gender=gender)
    return JSONResponse({"ok": True})


@app.post("/contacts/delete")
def contacts_delete(phone: str = Form(...)):
    db.delete_contact(phone.strip())
    return JSONResponse({"ok": True})


@app.post("/contacts/set-alias")
def contacts_set_alias(phone: str = Form(""), zalo_user_id: str = Form(""),
                       account_id: str = Form("default"), alias: str = Form("")):
    """Set the Zalo friend-alias (biệt danh) for a contact to its saved name.
    feature: customers with a name get that name as their Zalo alias."""
    c = db.get_contact_by_phone(phone.strip()) if phone.strip() else None
    uid = (zalo_user_id or (c or {}).get("zalo_user_id") or "").strip()
    name = (alias or (c or {}).get("display_name") or "").strip()
    ph = (phone or (c or {}).get("phone") or "").strip()
    desired = core.alias_for(name, ph)
    if not uid:
        return JSONResponse({"ok": False, "error": "chưa có Zalo ID — hãy tra SĐT trước"})
    if not desired:
        return JSONResponse({"ok": False, "skipped": True,
                             "error": "không có SĐT — giữ nguyên biệt danh"})
    return JSONResponse(core.set_friend_alias(account_id.strip() or "default", uid, desired))


@app.post("/contacts/set-alias-bulk")
def contacts_set_alias_bulk(account_id: str = Form("default"), file_id: str = Form(""),
                            tags: list[str] = Form([])):
    """Set alias for every contact (with a name) in a file or matching tags."""
    fid = int(file_id) if file_id.strip().isdigit() else None
    tag_list = _parse_tags(tags)
    if fid:
        src = db.list_file_members(fid)
    elif tag_list:
        src = db.list_contacts_by_tags(tag_list, 5000)
    else:
        src = db.list_contacts(5000)
    done = skip = 0
    for c in src:
        uid = (c.get("zalo_user_id") or "").strip()
        desired = core.alias_for((c.get("display_name") or "").strip(), (c.get("phone") or "").strip())
        if not uid or not desired:
            skip += 1
            continue
        try:
            r = core.set_friend_alias(account_id.strip() or "default", uid, desired)
            done += 1 if r.get("ok") else 0
            time.sleep(1)
        except Exception:
            skip += 1
    db.log_event("alias_bulk", f"set {done}, skip {skip}", account_id=account_id)
    return JSONResponse({"ok": True, "set": done, "skipped": skip})


@app.post("/preview-blocks")
def preview_blocks(blocks_json: str = Form("[]"), sample_name: str = Form("Minh")):
    """Render an ordered block list for a sample recipient (male/female), so the
    operator sees exactly how text/captions personalize before sending."""
    blocks = core.parse_block_list(blocks_json)

    def rend(g: str):
        rows = []
        for i, b in enumerate(blocks):
            t = b.get("type")
            if t == "text":
                txt = core.render_message(b.get("content") or "", name=sample_name, gender=g)
                rows.append({"i": i, "k": "text", "p": txt})
            elif t == "image":
                cap = core.render_message(b.get("caption") or "", name=sample_name, gender=g)
                rows.append({"i": i, "k": "image", "p": (cap + "  [🖼 ảnh]") if cap else "[🖼 ảnh]"})
            elif t == "link":
                cap = core.render_message(b.get("caption") or "", name=sample_name, gender=g)
                rows.append({"i": i, "k": "link", "p": (b.get("url") or "") + ((" — " + cap) if cap else "")})
        return rows

    return JSONResponse({"male": rend("male"), "female": rend("female")})


@app.post("/preview")
def preview(body_template: str = Form(""), sample_name: str = Form("Minh"), sample_gender: str = Form("")):
    return JSONResponse({
        "male": core.render_message(body_template, name=sample_name, gender="male"),
        "female": core.render_message(body_template, name=sample_name, gender="female"),
        "unknown": core.render_message(body_template, name=sample_name, gender=""),
    })


@app.post("/upload/image")
async def upload_image(file: UploadFile = File(...)):
    fn = file.filename or "img.jpg"
    if Path(fn).suffix.lower() not in IMAGE_EXTS:
        return JSONResponse({"ok": False, "error": "Chỉ nhận ảnh (jpg/png/webp/gif)"}, status_code=400)
    data = await file.read()
    if not data:
        return JSONResponse({"ok": False, "error": "Tệp rỗng"}, status_code=400)
    if len(data) > MAX_IMAGE:
        return JSONResponse({"ok": False, "error": f"Ảnh vượt {MAX_IMAGE // (1024*1024)}MB"}, status_code=413)
    dest = MEDIA_DIR / _safe_name(fn)
    dest.write_bytes(data)
    return JSONResponse({"ok": True, "src": str(dest), "url": f"/media/{dest.name}"})


# ----------------------------------------------------------------- campaigns

@app.post("/settings/save")
async def settings_save(request: Request):
    form = dict(await request.form())
    core.save_rate_settings(form)
    return JSONResponse({"ok": True})


@app.post("/contacts/resolve")
def contacts_resolve(account_id: str = Form("default"), only_missing: str = Form("1"),
                     file_id: str = Form("")):
    fid = int(file_id) if file_id.strip().isdigit() else None
    return JSONResponse(core.resolve_contacts(account_id.strip() or "default", only_missing == "1", file_id=fid))


@app.post("/groups/sync")
def groups_sync(account_id: str = Form("default")):
    return JSONResponse(core.sync_groups(account_id.strip() or "default"))


@app.post("/groups/{gid}/scan")
def groups_scan(gid: str, account_id: str = Form("default")):
    return JSONResponse(core.scan_group_members(account_id.strip() or "default", gid))


@app.get("/ui/group/{gid}/progress", response_class=HTMLResponse)
def ui_group_progress(request: Request, gid: str):
    return templates.TemplateResponse(request, "_group_progress.html",
                                      {"gid": gid, "p": core.scan_progress(gid)})


@app.get("/ui/scans", response_class=HTMLResponse)
def ui_scans(request: Request):
    return templates.TemplateResponse(request, "_scans.html",
                                      {"scans": db.scan_progress_recent(50)})


@app.post("/groups/{gid}/save-file")
def groups_save_file(gid: str, name: str = Form(""), account_id: str = Form("default")):
    members = db.list_members(gid, 5000)
    if not members:
        return JSONResponse({"ok": False, "error": "nhóm chưa có thành viên đã lấy — hãy bấm 'Quét thành viên' trước"})
    gname = next((g["name"] for g in db.list_groups(limit=2000) if g["group_id"] == gid), gid)
    fname = (name or "").strip() or f"{gname} - {time.strftime('%Y-%m-%d %H-%M')}"
    fid = db.create_contact_file(fname, source=f"group:{gid}")
    rows = [{"phone": m["user_id"], "zalo_user_id": m["user_id"], "display_name": m["display_name"]}
            for m in members]
    n = db.add_file_members(fid, rows)
    db.log_event("file_from_group", f"#{fid} {fname} <- group {gid}: +{n}", account_id=account_id)
    return JSONResponse({"ok": True, "file_id": fid, "name": fname, "added": n})


@app.post("/groups/{gid}/export")
def groups_export(gid: str, to_contacts: str = Form("1")):
    members = db.list_members(gid, 5000)
    if to_contacts == "1":
        rows = [{"phone": m["user_id"], "zalo_user_id": m["user_id"],
                 "display_name": m["display_name"], "tags": f"group:{gid}"} for m in members]
        n = db.add_contacts(rows)
        return JSONResponse({"ok": True, "exported": n})
    return JSONResponse({"ok": True, "count": len(members)})


@app.post("/campaigns/new")
async def campaigns_new(
    request: Request,
    name: str = Form(...),
    kind: str = Form("message"),
    account_id: str = Form("default"),
    thread_id: str = Form(""),
    thread_type: str = Form("group"),
    body_template: str = Form(""),
    url: str = Form(""),
    dry_run: str = Form("on"),
    use_contacts: str = Form("off"),
    file_id: str = Form(""),
    paste_targets: str = Form(""),
    tags: list[str] = Form([]),
    save_draft: str = Form(""),
    blocks_json: str | None = Form(None),
):
    tag_list = _parse_tags(tags)
    # Body is an ordered block list (text/image/link). Legacy forms fall back to
    # body_template + url + album images inside _campaign_blocks().
    blocks_raw = await _campaign_blocks(request, prev_json=blocks_json)
    resolved = core.parse_block_list(blocks_raw)
    body_template = core.normalize_text(
        next((b["content"] for b in resolved if b.get("type") == "text"), "") or body_template)
    img_paths = [b["src"] for b in resolved if b.get("type") == "image"]
    # A saved draft is always DRY (never sends); run it later with "Chạy nháp".
    dry = 1 if (save_draft or _is_dry(dry_run)) else 0
    cid = core.new_campaign(
        name=name.strip() or "campaign", kind=kind, account_id=account_id.strip() or "default",
        thread_id=thread_id.strip() or None, thread_type=thread_type,
        body_template=body_template, url=url.strip() or None,
        dry_run=dry, tags=json.dumps(tag_list, ensure_ascii=False),
        images=json.dumps(img_paths, ensure_ascii=False),
        blocks=blocks_raw,
    )
    added = 0
    fid = int(file_id) if file_id.strip().isdigit() else 0
    if file_id.strip() and fid:
        rows = [{"phone": c["phone"], "zalo_user_id": c["zalo_user_id"], "contact_id": c["id"]}
                for c in db.list_file_members(fid) if c["phone"]]
        added += db.add_targets(cid, rows)
    elif use_contacts == "on":
        src = db.list_contacts_by_tags(tag_list, 5000) if tag_list else db.list_contacts(5000)
        rows = [{"phone": c["phone"], "zalo_user_id": c["zalo_user_id"], "contact_id": c["id"]}
                for c in src if c["phone"]]
        added += db.add_targets(cid, rows)
    extra = _parse_phones(paste_targets)
    if extra:
        added += db.add_targets(cid, [{"phone": p} for p in extra])
    # Group-broadcast mode: a single thread_id means the SAME message goes to the
    # group, so N identical targets would just spam the group N times. Collapse
    # to ONE target no matter how the recipients were selected.
    group_mode = bool(thread_id.strip())
    if group_mode:
        db.trim_targets(cid, 1)
    db.log_event("campaign_new", f"{cid} {name} targets={added} tags={tag_list} imgs={len(img_paths)}"
                 + (" DRAFT" if save_draft else "")
                 + (" GROUP" if group_mode else ""),
                 account_id=account_id, campaign_id=cid)
    resp = {"ok": True, "campaign_id": cid, "targets": added,
            "draft": bool(save_draft),
            "tags": tag_list, "images": len(img_paths)}
    if group_mode:
        resp["warn"] = ("Chế độ gửi-nhóm: hệ thống tự rút về 1 người nhận (gửi 1 lần vào nhóm) "
                        "để tránh spam.")
    elif added == 0:
        resp["warn"] = ("Chiến dịch chưa có người nhận — hãy chọn Tệp KH, tick 'Toàn bộ danh bạ' "
                        "hoặc dán SĐT.")
    return JSONResponse(resp)


@app.post("/campaigns/from-file")
async def campaigns_from_file(
    request: Request,
    file_id: str = Form(...),
    name: str = Form(...),
    account_id: str = Form("default"),
    kind: str = Form("message"),
    body_template: str = Form(""),
    url: str = Form(""),
    dry_run: str = Form("on"),
    tags: list[str] = Form([]),
    blocks_json: str | None = Form(None),
):
    fid = int(file_id)
    tag_list = _parse_tags(tags)
    blocks_raw = await _campaign_blocks(request, prev_json=blocks_json)
    resolved = core.parse_block_list(blocks_raw)
    body_template = core.normalize_text(
        next((b["content"] for b in resolved if b.get("type") == "text"), "") or body_template)
    img_paths = [b["src"] for b in resolved if b.get("type") == "image"]
    members = db.list_file_members(fid)
    if tag_list:
        members = [c for c in members
                   if any(t in (c.get("tags") or "") for t in tag_list)]
    rows = [{"phone": c["phone"], "zalo_user_id": c["zalo_user_id"], "contact_id": c["id"]}
            for c in members if c["phone"]]
    cid = core.new_campaign(
        name=name.strip() or f"file {fid}", kind=kind, account_id=account_id.strip() or "default",
        thread_id=None, thread_type="user", body_template=body_template, url=url.strip() or None,
        dry_run=1 if _is_dry(dry_run) else 0, tags=json.dumps(tag_list, ensure_ascii=False),
        images=json.dumps(img_paths, ensure_ascii=False),
        blocks=blocks_raw,
    )
    added = db.add_targets(cid, rows)
    db.log_event("campaign_new", f"{cid} from file {fid} targets={added} tags={tag_list} imgs={len(img_paths)}",
                 campaign_id=cid)
    return JSONResponse({"ok": True, "campaign_id": cid, "targets": added,
                         "tags": tag_list, "images": len(img_paths)})


@app.post("/campaigns/from-group")
async def campaigns_from_group(
    request: Request,
    name: str = Form(...),
    gid: str = Form(...),
    account_id: str = Form("default"),
    kind: str = Form("message"),
    body_template: str = Form(""),
    url: str = Form(""),
    dry_run: str = Form("on"),
    tags: list[str] = Form([]),
    blocks_json: str | None = Form(None),
):
    tag_list = _parse_tags(tags)
    blocks_raw = await _campaign_blocks(request, prev_json=blocks_json)
    resolved = core.parse_block_list(blocks_raw)
    body_template = core.normalize_text(
        next((b["content"] for b in resolved if b.get("type") == "text"), "") or body_template)
    img_paths = [b["src"] for b in resolved if b.get("type") == "image"]
    members = db.list_members(gid, 5000)
    rows = [{"phone": m["user_id"], "zalo_user_id": m["user_id"]} for m in members]
    cid = core.new_campaign(
        name=name.strip() or f"group {gid}", kind=kind, account_id=account_id.strip() or "default",
        thread_id=None, thread_type="user", body_template=body_template, url=url.strip() or None,
        dry_run=1 if _is_dry(dry_run) else 0, tags=json.dumps(tag_list, ensure_ascii=False),
        images=json.dumps(img_paths, ensure_ascii=False),
        blocks=blocks_raw,
    )
    added = db.add_targets(cid, rows)
    # members carry their zalo id as the 'phone' key here; fill via contacts too
    for m in members:
        db.add_contacts([{"phone": m["user_id"], "zalo_user_id": m["user_id"],
                          "display_name": m["display_name"], "tags": f"group:{gid}"}])
    db.log_event("campaign_new", f"{cid} from group {gid} targets={added}", campaign_id=cid)
    return JSONResponse({"ok": True, "campaign_id": cid, "targets": added})


@app.get("/ui/campaign/{cid}/edit", response_class=HTMLResponse)
def ui_campaign_edit(request: Request, cid: str):
    camp = db.get_campaign(cid)
    if not camp:
        return HTMLResponse("<div class='mut'>Không tìm thấy chiến dịch.</div>")
    return templates.TemplateResponse(request, "_campaign_edit.html", {
        "camp": camp, "campaigns": db.list_campaigns(), "accounts": db.list_accounts(),
        "files": db.list_contact_files(), "all_tags": db.all_tags(),
        "tcount": db.campaign_counts(cid).get("total", 0),
        "blocks_json": core.blocks_to_json(core.parse_blocks(camp)),
    })


async def _apply_campaign_edit(request: Request, cid: str):
    """Shared edit handler: update whitelisted fields; if a new file / tag
    filter / pasted phones is provided, (re)build the target list."""
    camp = db.get_campaign(cid)
    if not camp:
        return JSONResponse({"ok": False, "error": "campaign not found"}, status_code=404)
    form = await request.form()

    fields: dict = {
        "name": (form.get("name") or camp["name"] or "campaign").strip(),
        "kind": form.get("kind") if form.get("kind") in ("message", "friend") else camp["kind"],
        "account_id": (form.get("account_id") or camp["account_id"] or "default").strip(),
        "thread_id": (form.get("thread_id") or "").strip() or None,
        "url": (form.get("url") or "").strip() or None,
        "dry_run": 1 if _is_dry(form.get("dry_run", "on")) else 0,
    }
    # Body content: if the form carries the new block builder, it wins (and we
    # keep body_template/images in sync for legacy views); otherwise keep any
    # legacy body_template edit as before.
    if "blocks_json" in form:
        blocks_raw = await _campaign_blocks(request, prev_json=camp.get("blocks") or core.blocks_to_json(core.parse_blocks(camp)))
        resolved = core.parse_block_list(blocks_raw)
        fields["blocks"] = blocks_raw
        fields["images"] = json.dumps([b["src"] for b in resolved if b.get("type") == "image"], ensure_ascii=False)
        fields["body_template"] = core.normalize_text(
            next((b["content"] for b in resolved if b.get("type") == "text"), ""))
        fields["url"] = next((b["url"] for b in resolved if b.get("type") == "link"), None)
    elif form.get("body_template") is not None:
        fields["body_template"] = core.normalize_text(form.get("body_template"))
    if "tags" in form:
        sel = _parse_tags(form.getlist("tags"))
        if sel:
            fields["tags"] = json.dumps(sel, ensure_ascii=False)
    else:
        new_imgs = await _save_campaign_uploads(request)
        if new_imgs:
            fields["images"] = json.dumps(new_imgs, ensure_ascii=False)
            # Keep the block list consistent with the swapped album (legacy path:
            # text first, then the new album, then the optional link).
            rebuilt = ([{"type": "text", "content": fields.get("body_template")}] if fields.get("body_template") else []) \
                + [{"type": "image", "src": p, "caption": ""} for p in new_imgs] \
                + ([{"type": "link", "url": fields.get("url"), "caption": ""}] if fields.get("url") else [])
            fields["blocks"] = core.blocks_to_json(rebuilt)

    db.update_campaign(cid, **fields)
    db.log_event("campaign_edit", f"{cid} {fields.get('name')}", campaign_id=cid)
    return JSONResponse({"ok": True, "campaign_id": cid})


@app.post("/campaigns/{cid}/edit")
async def campaigns_edit(request: Request, cid: str):
    return await _apply_campaign_edit(request, cid)


@app.post("/campaigns/{cid}/update")
async def campaigns_update(request: Request, cid: str):
    """Edit + rebuild the target list (clear old targets, re-add from file/tags/paste)."""
    camp = db.get_campaign(cid)
    if not camp:
        return JSONResponse({"ok": False, "error": "campaign not found"}, status_code=404)
    resp = await _apply_campaign_edit(request, cid)
    if resp.status_code != 200:
        return resp
    form = await request.form()
    file_id = (form.get("file_id") or "").strip()
    paste = (form.get("paste_targets") or "").strip()
    tag_sel = _parse_tags(form.getlist("tags")) if "tags" in form else []
    use_contacts = form.get("use_contacts") == "on"

    # Only rebuild the target list when the user explicitly picks a new source.
    if file_id or paste or use_contacts or tag_sel:
        db.clear_targets(cid)
        added = 0
        fid = int(file_id) if file_id.isdigit() else 0
        if fid:
            rows = [{"phone": c["phone"], "zalo_user_id": c["zalo_user_id"], "contact_id": c["id"]}
                    for c in db.list_file_members(fid) if c["phone"]]
            if tag_sel:
                rows = [r for r in rows
                        if any(t in (db.get_contact_by_phone(r["phone"]) or {}).get("tags", "") for t in tag_sel)]
            added += db.add_targets(cid, rows)
        elif use_contacts or tag_sel:
            src = db.list_contacts_by_tags(tag_sel, 5000) if tag_sel else db.list_contacts(5000)
            added += db.add_targets(cid, [{"phone": c["phone"], "zalo_user_id": c["zalo_user_id"], "contact_id": c["id"]} for c in src if c["phone"]])
        paste_phones = _parse_phones(paste)
        if paste_phones:
            added += db.add_targets(cid, [{"phone": p} for p in paste_phones])
        # Group-broadcast mode: collapse to a single target (same thread).
        # `fields` lives inside _apply_campaign_edit, not here, so read the
        # submitted form value directly (fixes NameError on this endpoint).
        if (form.get("thread_id") or "").strip():
            db.trim_targets(cid, 1)
        db.log_event("campaign_targets", f"{cid} rebuilt targets={added}", campaign_id=cid)
    return JSONResponse({"ok": True, "campaign_id": cid})


@app.post("/campaigns/{cid}/build")
def campaigns_build(cid: str):
    n = core.build_campaign_targets(cid)
    return JSONResponse({"ok": True, "resolved": n})


@app.post("/campaigns/{cid}/start")
def campaigns_start(cid: str):
    return JSONResponse(core.start_campaign(cid))


@app.post("/campaigns/{cid}/pause")
def campaigns_pause(cid: str):
    core.pause_campaign(cid, True)
    return JSONResponse({"ok": True})


@app.post("/campaigns/{cid}/resume")
def campaigns_resume(cid: str):
    return JSONResponse(core.resume_campaign(cid))


@app.post("/campaigns/{cid}/stop")
def campaigns_stop(cid: str):
    return JSONResponse(core.stop_campaign(cid))


@app.post("/campaigns/{cid}/delete")
def campaigns_delete(cid: str):
    return JSONResponse(core.delete_campaign(cid))


@app.post("/campaigns/{cid}/dry")
def campaigns_dry(cid: str, dry: str = Form("1")):
    return JSONResponse(core.set_campaign_dry(cid, dry == "1"))


@app.post("/campaigns/{cid}/retry")
def campaigns_retry(cid: str, statuses: str = Form("failed")):
    wanted = tuple(s.strip() for s in statuses.split(",") if s.strip()) or ("failed",)
    return JSONResponse(core.retry_failed(cid, wanted))


@app.post("/killswitch")
def killswitch(on: str = Form("0")):
    core.set_killswitch(on == "1")
    db.log_event("killswitch", on)
    return JSONResponse({"ok": True, "on": on == "1"})


# ------------------------------------------------------------------- autobot

def _group_name(gid: str) -> str:
    gid = (gid or "").strip()
    for g in db.list_groups(limit=5000):
        if g["group_id"] == gid:
            return g.get("name") or gid
    return gid


@app.get("/ui/autobot", response_class=HTMLResponse)
def ui_autobot(request: Request):
    return templates.TemplateResponse(request, "_autobot_rules.html", {
        "request": request,
        "rules": db.list_forward_rules(),
        "bots": {b["account_id"]: b for b in core.bot_status()},
        "accounts": db.list_accounts(),
        "groups": db.list_groups(limit=5000),
        "kill": core.killswitch_active(),
    })


@app.get("/ui/autobot/log", response_class=HTMLResponse)
def ui_autobot_log(request: Request, rule_id: str = ""):
    return templates.TemplateResponse(request, "_autobot_log.html", {
        "request": request,
        "log": db.list_forward_log(200, rule_id or None),
        "rule_id": rule_id,
    })


@app.get("/ui/autobot/status")
def ui_autobot_status():
    return JSONResponse({"ok": True, **core.autobot_snapshot()})


@app.post("/autobot/new")
def autobot_new(name: str = Form(""), account_id: str = Form(""),
                src_group_id: str = Form(""), dst_group_id: str = Form(""),
                prefix: str = Form(""), max_per_hour: str = Form("60"),
                gap_seconds: str = Form("5"), group_window_ms: str = Form("2000"),
                reply_mode: str = Form("text"), reply_quote_ms: str = Form("0"),
                dry_run: str = Form("on"),
                include_self: str = Form("0"), enabled: str = Form("1")):
    acc = account_id.strip()
    src = src_group_id.strip()
    dst = dst_group_id.strip()
    if not acc:
        return JSONResponse({"ok": False, "error": "Chọn tài khoản Zalo"}, status_code=400)
    if not src or not dst:
        return JSONResponse({"ok": False, "error": "Chọn nhóm nguồn (A) và nhóm đích (B)"}, status_code=400)
    if src == dst:
        return JSONResponse({"ok": False, "error": "Nhóm A và B phải khác nhau"}, status_code=400)
    rid = "f_" + uuid.uuid4().hex[:8]
    db.create_forward_rule(
        rid, name=name.strip() or "Luồng mới", account_id=acc,
        src_group_id=src, src_group_name=_group_name(src),
        dst_group_id=dst, dst_group_name=_group_name(dst),
        prefix=prefix, enabled=1 if _last_bool(enabled, True) else 0,
        dry_run=1 if _is_dry(dry_run) else 0,
        include_self=1 if _last_bool(include_self, False) else 0,
        max_per_hour=max(1, min(int(max_per_hour or 60), 100000)),
        gap_seconds=max(0, min(int(gap_seconds or 5), 86400)),
        group_window_ms=max(0, min(int(group_window_ms or 2000), 60000)),
        reply_mode=_reply_mode(reply_mode), reply_quote_ms=1 if _last_bool(reply_quote_ms, False) else 0)
    db.log_event("autobot_rule_new", f"{rid} {src}->{dst}", account_id=acc)
    auto = ""
    if _last_bool(enabled, True):
        r = core.start_bot(acc)  # no-op if already running
        core._bot_apply_watch(acc)  # refresh allowlist even if bot was already up
        auto = " · bot đang nghe" if r.get("ok") else f" · bot lỗi: {r.get('error')}"
    return JSONResponse({"ok": True, "rule_id": rid, "note": auto})


@app.post("/autobot/{rid}/update")
def autobot_update(rid: str, name: str = Form(""), src_group_id: str = Form(""),
                   dst_group_id: str = Form(""), prefix: str = Form(""),
                   max_per_hour: str = Form("60"), gap_seconds: str = Form("5"),
                   group_window_ms: str = Form("2000"),
                   reply_mode: str = Form("text"), reply_quote_ms: str = Form("0"),
                   dry_run: str = Form("on"), include_self: str = Form("0"),
                   enabled: str = Form("1")):
    rule = db.get_forward_rule(rid)
    if not rule:
        return JSONResponse({"ok": False, "error": "không tìm thấy luồng"}, status_code=404)
    src = src_group_id.strip() or rule["src_group_id"]
    dst = dst_group_id.strip() or rule["dst_group_id"]
    if src == dst:
        return JSONResponse({"ok": False, "error": "Nhóm A và B phải khác nhau"}, status_code=400)
    db.update_forward_rule(
        rid, name=name.strip() or rule["name"],
        src_group_id=src, src_group_name=_group_name(src),
        dst_group_id=dst, dst_group_name=_group_name(dst),
        prefix=prefix, enabled=1 if _last_bool(enabled, True) else 0,
        dry_run=1 if _is_dry(dry_run) else 0,
        include_self=1 if _last_bool(include_self, False) else 0,
        max_per_hour=max(1, min(int(max_per_hour or 60), 100000)),
        gap_seconds=max(0, min(int(gap_seconds or 5), 86400)),
        group_window_ms=max(0, min(int(group_window_ms or 2000), 60000)),
        reply_mode=_reply_mode(reply_mode), reply_quote_ms=1 if _last_bool(reply_quote_ms, False) else 0)
    db.log_event("autobot_rule_update", rid, account_id=rule["account_id"])
    if _last_bool(enabled, True):
        core.start_bot(rule["account_id"])
    core._bot_apply_watch(rule["account_id"])
    return JSONResponse({"ok": True})


@app.post("/autobot/{rid}/delete")
def autobot_delete(rid: str):
    rule = db.get_forward_rule(rid)
    db.delete_forward_rule(rid)
    db.log_event("autobot_rule_delete", rid)
    # if no rules remain for that account, stop its bot; otherwise refresh the
    # watch allowlist so the freed-up source group is no longer listened to
    if rule:
        remain = [r for r in db.list_forward_rules() if r.get("account_id") == rule["account_id"] and r.get("enabled")]
        if not remain:
            core.stop_bot(rule["account_id"])
        else:
            core._bot_apply_watch(rule["account_id"])
    return JSONResponse({"ok": True})


@app.post("/autobot/{rid}/toggle")
def autobot_toggle(rid: str, enabled: str = Form("1")):
    rule = db.get_forward_rule(rid)
    if not rule:
        return JSONResponse({"ok": False, "error": "không tìm thấy luồng"}, status_code=404)
    on = enabled == "1"
    db.update_forward_rule(rid, enabled=1 if on else 0)
    if on:
        core.start_bot(rule["account_id"])
    else:
        remain = [r for r in db.list_forward_rules()
                  if r.get("account_id") == rule["account_id"] and r.get("enabled") and r["id"] != rid]
        if not remain:
            core.stop_bot(rule["account_id"])
    db.log_event("autobot_rule_toggle", f"{rid}={'on' if on else 'off'}")
    core._bot_apply_watch(rule["account_id"])
    return JSONResponse({"ok": True, "enabled": on})


@app.post("/autobot/bot/start")
def autobot_bot_start(account_id: str = Form("")):
    return JSONResponse(core.start_bot(account_id.strip()))


@app.post("/autobot/bot/stop")
def autobot_bot_stop(account_id: str = Form("")):
    return JSONResponse(core.stop_bot(account_id.strip()))


@app.post("/autobot/bot/restart")
def autobot_bot_restart(account_id: str = Form("")):
    return JSONResponse(core.restart_bot(account_id.strip()))


@app.post("/autobot/bot/backfill")
def autobot_bot_backfill(account_id: str = Form(""), cursor: str = Form("")):
    """Replay history missed while the listener was down (idempotent, safe).

    Optional `cursor` overrides the replay cut-off (testing/ops).
    """
    return JSONResponse(core.bot_backfill(account_id.strip(), cursor_override=cursor.strip()))


@app.post("/autobot/bot/send")
def autobot_bot_send(account_id: str = Form("nick1"), thread: str = Form(""),
                     parts_json: str = Form("[]"), prefix: str = Form(""),
                     reply_prefix: str = Form(""), quote_json: str = Form(""),
                     dry: str = Form("1")):
    """Internal: relay a canonical part post through the running bot so the
    bridge's real normalize/coalesce/send path can be verified live. DRY-RUN by
    default; pass dry=0 to actually send. reply_prefix adds a '↩ …' context line;
    quote_json (optional) attaches a native Zalo quote."""
    try:
        parts = json.loads(parts_json or "[]")
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"bad parts_json: {e}"}, status_code=400)
    quote = None
    if quote_json.strip():
        try:
            quote = json.loads(quote_json)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"bad quote_json: {e}"}, status_code=400)
    if not thread:
        return JSONResponse({"ok": False, "error": "thread required"}, status_code=400)
    cmd = {"cmd": "send", "thread": thread, "type": "group",
           "dryRun": str(dry).strip() not in ("0", "off", "false"),
           "parts": parts, "prefix": prefix, "reply_prefix": reply_prefix}
    if quote:
        cmd["quote"] = quote
    return JSONResponse(core._bot_cmd(account_id, cmd, timeout=60))


@app.post("/autobot/bot/watch")
def autobot_bot_watch(account_id: str = Form("")):
    """Re-push the watch allowlist (enabled rules' source groups) to a worker."""
    acc = account_id.strip()
    res = core._bot_apply_watch(acc)
    groups = core._bot_watch_groups(acc)
    return JSONResponse({"ok": bool(res.get("ok", True)), "account": acc,
                         "groups": groups, "detail": res})


@app.get("/healthz")
def healthz():
    return {"ok": True, "killswitch": core.killswitch_active(), "queue": core.queue_depth()}
