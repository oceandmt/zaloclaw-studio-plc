# zaloclaw-studio

Web app quản lý marketing Zalo nội bộ (kiểu akaBiz) chạy trên nền **zca-js** — tài khoản Zalo cá nhân.
Dùng nội bộ, 1–3 nick, **chấp nhận rủi ro khóa nick** (zca-js là unofficial → vi phạm ToS Zalo).

## Kiến trúc

```
webapp/  (FastAPI + HTMX + SQLite)   ← control panel: login QR, tệp KH, chiến dịch, log, killswitch
bridge/  (Node + zca-js)             ← sessiond.mjs: 1 session Zalo/nick; zalo.mjs: login-qr, send, find-users…
config/settings.json                 ← rate-limit + safety defaults
data/                                ← zs.db, accounts/<id>.json (creds 0600), sessiond-*.sock, KILLSWITCH
deploy/                              ← systemd templates + install.sh (Option A), env example
```

- **Chạy 2 role tách biệt** (khuyến nghị cho production): cùng 1 codebase, phân vai bằng biến `ZS_ROLE`:
  - `campaign` → **panel 18090** (chiến dịch, tệp KH, tài khoản) — KHÔNG chạy listener G2G
  - `g2g` → **pipeline 18091** (chuyển tiếp nhóm A→B, listener, backfill) — KHÔNG chạy campaign worker
  - `all` (mặc định) → 1 tiến trình gánh cả hai (chế độ test / máy đơn).
  - Hai role **chia sẻ CHUNG** `data/`: 1 SQLite DB (`zs.db`) + 1 session daemon/nick
    (`data/sessiond-<nick>.sock`) → không bao giờ có 2 session Zalo tranh nhau cùng 1 nick.
  - Flask/uvicorn **không hot-reload**: sửa code phải restart service.
- **Không cần OpenClaw**: bridge tự login bằng credentials riêng (`data/accounts/<id>.json`).
- **DRY-RUN mặc định**: chiến dịch mới tạo luôn dry_run=1 → không gửi thật cho tới khi bấm "Bật LIVE".
- **Rate-limit** per-account: max/giờ, max/ngày, kết bạn/ngày, min-gap + jitter.
- **KILLSWITCH**: nút "DỪNG KHẨN" chặn mọi lượt gửi thật ngay lập tức.

## Repo (private / public)

- **`oceandmt/zaloclaw-studio`** — **PRIVATE**. Bản chạy thật của máy này, nguồn backup/push/commit.
  Chứa PII khách hàng (số ĐT, tên, ID nhóm Zalo) trong test + tài liệu nội bộ ⇒ KHÔNG publish.
- **`oceandmt/zaloclaw-studio-plc`** — **PUBLIC**. Bản để **cài máy mới** — cùng codebase, đã **lọc sạch PII**
  (ID nhóm/uid/SĐT/tên khách → placeholder) và bỏ tài liệu nội bộ (AUDIT…).

Đồng bộ public = export snapshot HEAD → sanitize → test → push:

```bash
scripts/publish-public.sh "chore(public): sync ..."
```

`scripts/sanitize_for_public.py` thay mọi định danh khách (bảng `REPLACEMENTS`) rồi **assert 0 PII**
(exit ≠ 0 sẽ chặn push). Máy mới: clone repo **public** rồi làm mục *Cài đặt máy mới* bên dưới.

## Cài đặt máy mới

**Yêu cầu chung:** Linux, `python3` (kèm `venv`), `node` ≥ 18 + `npm`, và internet **lần đầu**
(tải zca-js). Muốn bind IP Tailscale thì cài thêm Tailscale (`tailscale ip -4` để lấy IP).

> ⚠️ `data/` chứa **credentials Zalo + PII khách hàng** → KHÔNG bao giờ commit/publish.
> Máy mới phải **đăng nhập QR lại** (mỗi nick 1 lần).

### Option A — systemd `--user` (khớp production hiện tại)

```bash
cd <nơi-clone>/zaloclaw-studio
deploy/install.sh                       # mặc định HOST=127.0.0.1, panel 18090, g2g 18091
deploy/install.sh --host 0.0.0.0        # hoặc --host $(tailscale ip -4) để mở qua Tailscale
deploy/install.sh --no-start            # chỉ cài, chưa start
```

`install.sh` (idempotent) sẽ: tạo `webapp/.venv` + pip deps, `npm ci` cho `bridge/`,
tạo `data/` + `logs/`, render 2 unit vào `~/.config/systemd/user/`, rồi
`systemctl --user enable --now` + `loginctl enable-linger` (chạy nền qua reboot, không cần đăng nhập).

- Panel: `http://<HOST>:18090/` · G2G: `http://<HOST>:18091/`
- Log: `journalctl --user -u zaloclaw-studio-panel -f` · `... -u zaloclaw-g2g -f`

### Option B — Docker Compose (máy nào có Docker cũng chạy)

```bash
cd <nơi-clone>/zaloclaw-studio
docker compose build
HOST=127.0.0.1 docker compose up -d          # local only
HOST=$(tailscale ip -4) docker compose up -d # mở qua Tailscale
```

2 container (`panel`=18090, `g2g`=18091) **share volume `./data`** (DB + session + creds)
→ cùng quy tắc 1-session/nick như Option A. `HOST` là **bind IP trên máy host**.

### Chạy nhanh không cần service (dev / máy đơn)

```bash
python3 -m venv webapp/.venv && webapp/.venv/bin/pip install -r requirements.txt
(cd bridge && npm install)                     # cần internet lần đầu

scripts/run_webapp.sh                          # mặc định 127.0.0.1:18090 (đặt ZS_HOST/ZS_PORT để đổi)
ZS_ROLE=all scripts/run_webapp.sh 0.0.0.0 18090

# kiểm tra JS frontend (bắt lỗi sai nháy / token không phải JS trong <script>)
scripts/check_frontend.sh http://127.0.0.1:18090
```

**Lần đầu:** mở panel → tab **Tài khoản** → **Thêm tài khoản** → quét QR Zalo.
Creds lưu ở `data/accounts/<nick>.json` (0600).

## Giao diện

UI chia thành **6 tab menu** (không còn 1 trang dài):
**Tài khoản · Tệp KH · Nhóm · Chiến dịch · Log · Quét nhóm**. Mỗi tab nạp qua HTMX (`/ui/tab/<name>`),
modal QR + modal nhóm nằm ở cấp trang nên không bị mất khi chuyển tab.

### Quản lý tài khoản Zalo (tab Tài khoản)
Trung tâm quản trị **nhiều nick Zalo** trong một panel:
- **➕ Thêm tài khoản** → mã QR hiện ngay → quét bằng app Zalo.
- **Trạng thái từng nick**: `đã đăng nhập` / `chờ quét QR` / `hết phiên` / `lỗi` / `đang tắt`; cảnh báo **⚠ lệch creds** khi DB có thông tin nhưng thiếu file đăng nhập.
- **Hạn mức hôm nay** (mỗi nick): `✉ tin/giờ · tin/ngày · 🤝 kết bạn/ngày · 🔍 quét/ngày` dạng *đã dùng / trần*. Chấm **vàng** = sắp chạm trần, **đỏ** = đã chạm trần (nick tạm ngừng gửi để tránh khóa).
- **Gần nhất**: thời điểm **gửi** và **quét** cuối cùng (biết nick nào đang "nguội" cần warm-up).
- **Nút thao tác**: **🔌 Kiểm tra** (thử phiên thật → gắn `hết phiên` nếu chết) · **🔄** (cập nhật tên/ID) · **✏️** (đổi tên nick) · **⏸/▶** (tạm **tắt/bật** một nick mà không xoá) · **Đăng nhập QR** · **🗑** (xoá nick, tuỳ chọn xoá luôn file creds).
- **Chốt an toàn**: nick **đang tắt** hoặc **chưa đăng nhập** sẽ **không** chạy chiến dịch LIVE và **không** quét nhóm — job được giữ lại, không đốt chiến dịch.
- Badge header hiển thị **nick bật: x/y**.

### Tiến độ quét nhóm (live)
Quét thành viên chạy **nền (non-blocking)** — bấm là trả về ngay, có:
- **Thanh tiến độ** ngay trên bảng nhóm (tự poll mỗi 2s, tự dừng khi xong).
- **Log live** (tab Log): `SCAN start / info / chunk N/M / done`.
- **Tab "Quét nhóm"**: lịch sử các lượt quét (trạng thái: đang quét / đủ / một phần / bị chặn / lỗi) + thời gian.

### Tệp khách hàng (nhiều tệp)
Tab **Tệp KH** quản lý **nhiều tệp KH riêng** (không còn 1 danh sách phẳng):
- **Tạo tệp** đặt tên, hoặc **dán SĐT** vào tệp.
- **Lưu kết quả nhóm đã quét thành tệp** (nút 💾 trong modal nhóm) hoặc lấy từ nhóm trong trang tệp.
- **Trong mỗi tệp**: ✏️ Sửa (tên/giới tính), ➖ Gỡ khỏi tệp, 🗑 Xóa hẳn SĐT; 🔎 Tra tên+giới tính; 🎯 Tạo chiến dịch từ tệp.
- **CSV**: 📤 Tải CSV lên (tạo tệp mới hoặc thêm vào tệp có sẵn), 📥 Xuất tệp ra CSV (+ xuất toàn bộ liên hệ).
  CSV nhận cột `phone,name,gender` hoặc `sđt,tên,giới tính` (tự nhận delimiter `, ; tab`, có/không header, xuất kèm BOM để Excel đọc UTF-8).
- Bảng `contact_files` + `file_members` (1 SĐT có thể thuộc nhiều tệp).
- Chọn tệp khi tạo chiến dịch (dropdown "Nguồn SĐT").

### Tự lấy tên + giới tính từ SĐT
Tệp KH chỉ có SĐT (hoặc **quét từ nhóm**) vẫn gửi được tin cá nhân hoá:
- Nút **🔎 Tra tên + giới tính** (tab Tệp KH) tra Zalo và tự điền `display_name` + `gender` cho KH còn trống.
- **Hai chế độ tra tự động**: KH dạng **SĐT** → tra bằng API theo SĐT (`find-users`); KH **quét từ nhóm** (cột SĐT thực chất là **Zalo user id**) → tra bằng API **theo userId** (`user-info`/`getUserInfo`) — **không còn bỏ sót giới tính** của KH nhóm.
- **Tự tra trước khi gửi**: khi chạy campaign, hệ thống tự tra các SĐT chưa có tên (tắt được ở tab Cài đặt).
- Fallback tên: nếu không tra được, lấy tên đã lưu từ nhóm đã quét.
- Nguồn: Zalo `getMultiUsersByPhones` trả `display_name` + `gender` (0=nam, 1=nữ).

### Tần suất gửi / nghỉ (tab Cài đặt)
Chỉnh trực tiếp trên UI (lưu vào DB, áp dụng ngay): tối đa tin/giờ, tin/ngày, kết bạn/ngày,
**nghỉ tối thiểu giữa 2 tin** (`min_gap_seconds`) + **jitter** ngẫu nhiên, và toàn bộ hạn mức quét nhóm.
An toàn: worker xử lý **tuần tự** (1 tin/lúc) nên nhiều chiến dịch cũng không vượt hạn mức.

### Chiến dịch
- **Nội dung dạng KHỐI (block) — sắp xếp tuỳ ý**: thay vì `1 text + 1 album`, nội dung là **danh sách khối** gửi **tuần tự** theo thứ tự:
  - **＋ Chữ** = 1 tin nhắn text
  - **＋ Ảnh** = gửi ảnh; **các khối Ảnh liền nhau** gộp thành **1 album** (mỗi ảnh có **chú thích riêng**)
  - **＋ Link** = thẻ link (kèm chú thích tuỳ chọn)
  - Mỗi khối có nút **▲ ▼ ✕** để **kéo lên/xuống/xoá**; ảnh tải lên **ngay khi chọn**; nút **👁 Xem thử** render bản nam/nữ.
  - Placeholder `{name}` / `{salutation}` áp cho cả **chữ** và **chú thích ảnh/link**.
  - Lưu ở cột `campaigns.blocks` (JSON). **Tương thích ngược**: chiến dịch cũ (chỉ có body+album) tự chuyển thành 1 khối Chữ rồi album, không cần migrate.
- Nút: **Resolve SĐT · ✏️ Sửa · Chạy/Tiếp tục · Dừng · Bật LIVE/Về DRY · 🗑 Xóa**.
- **Nguồn người nhận (an toàn)**: chọn **Tệp KH**, tick **Dùng TOÀN BỘ danh bạ** (mặc định **TẮT**), và/hoặc **dán SĐT**. Dán SĐT nhận **dòng / dấu phẩy / chấm phẩy / khoảng trắng** và **nhiều SĐT cùng lúc**, và cả dạng `SĐT, Tên, Giới tính` (chỉ SĐT được lấy). Không chọn gì → 0 người nhận + cảnh báo (tránh gửi nhầm cho tất cả).
- **Lưu nháp + chỉnh sửa từng chiến dịch**:
  - **💾 Lưu nháp (chưa chạy)**: lưu chiến dịch nghi **DRY** (trạng thái `nháp`, không gửi) — quay lại chạy sau bằng **▶ Chạy nháp**.
  - **✏️ Sửa**: mở form chỉnh **tên, loại, TK gửi, nhóm đích, nội dung, link, ảnh, DRY/LIVE**. Phần **🎯 Đổi nguồn SĐT** (tệp/tag/dán) chỉ **rebuild danh sách** khi bạn thực sự chọn nguồn mới; đĐ trống = **giữ nguyên** danh sách hiện có. Có nút **Xem thử**.
- **Gửi nhiều ảnh**: dùng khối **Ảnh** (xem trên). Ảnh lưu ở `data/media/`, phục vụ qua `/media/…`, thumbnail hiện trên danh sách chiến dịch.
- **Pause/Resume an toàn**: bấm Dừng rồi bấm Chạy/Tiếp tục hoạt động đúng (không còn kẹt). Tự đánh dấu *xong* khi gửi hết. Chống bấm đúp.
- **Xuống dòng đúng như bản gốc**: trình duyệt gửi textarea bằng CRLF (`\r\n`), mà Zalo coi **cả `\r` lẫn `\n`** là 1 lần xuống dòng → chữ bị **giãn dòng gấp đôi**. Hệ thống tự chuẩn hoá mọi kiểu xuống dòng về **1 `\n`** (CRLF/CR/U+2028 → `\n`, bỏ khoảng trắng cuối dòng), **giữ nguyên dòng trống cố ý** (`\n\n`). Chuẩn hoá ở 3 lớp: khi lưu (`core.normalize_text`), khi render (`render_message`), và khi gửi (`normText` trong `bridge/zalo.mjs`) — nên cả chiến dịch cũ cũng tự sạch.
- **Xóa chiến dịch** kèm toàn bộ SĐT của nó (có confirm).

## Quy trình sử dụng

1. **Tài khoản** → bấm **➕ Thêm tài khoản Zalo** → mã QR hiện ra ngay → quét bằng app Zalo.
   (Không cần nhập ID — hệ thống tự đặt nick1, nick2…)
2. **Nhóm** → chọn TK → **Quét danh sách nhóm** → xem nhóm → **Quét thành viên**.
   - Từ 1 nhóm: **Xuất vào Tệp KH** hoặc **Tạo chiến dịch từ nhóm** (gửi tin/kết bạn cho thành viên).
3. **Tệp khách hàng** → dán SĐT (`SĐT, Tên, Giới tính`) → Nhập danh sách.
3. **Chiến dịch** → chọn loại (Gửi tin / Kết bạn), TK gửi, **nội dung dạng khối** (thêm Chữ / Ảnh / Link, sắp xếp ▲▼). Dùng placeholder cá nhân hoá:
   - `{name}` → tên người nhận
   - `{salutation}` → **Anh / Chị** tự động theo giới tính (VD: `Chào {salutation} {name}`)
   - `{gioi_tinh}` → nam/nữ
   - Bấm **Xem thử** để xem 3 bản render (nam/nữ/khác).
4. **Resolve SĐT** → tra SĐT ra userId Zalo (bắt buộc trước khi gửi cho KH).
5. **Chạy** (đang DRY) → xem log `DRY send`.
6. Ưng thì bấm **Bật LIVE** rồi Chạy lại.
7. Có sự cố → **DỪNG KHẨN**.

### 🔀 Pipeline G2G (chuyển tiếp nhóm A → nhóm B)
- Tab **🔀 Pipeline G2G**: tạo **pipeline** chọn **Nhóm A (nguồn, LISTEN)** và **Nhóm B (đích, nhận tin)** cùng **tài khoản Zalo** vừa nghe vừa gửi.
- **🖼 Tin có ẢNH + text:** caption của ảnh Zalo nằm ở `content.title` (payload thật KHÔNG có key `caption`) — bridge dùng `relay.mediaText()` đọc theo thứ tự title→description→content, rồi gửi 1 block `image` kèm caption (nếu tải ảnh lỗi mới tách text riêng). Bật `ZS_MEDIA_DUMP=<path>` để dump nguyên object tin media phục vụ debug.
- **🧩 Chuẩn hoá mọi loại tin (canonical parts):** mọi tin Zalo → 1 **Part** `{kind: text|image|gif|video|file|voice|sticker|link|location|recommended}` (`relay.normalizePart`). Tin hệ thống (thu hồi/xoá/reaction/join/seen) → **bỏ qua** (`null`). Bridge emit `part` kèm `msg`.
- **🔗 Gom cụm (grouping):** ảnh + text gửi **rời** (2 tin liên tiếp cùng người gửi trong `group_window_ms`) → **gộp 1 tin** (`coalesceParts`: ảnh rỗng caption + text kế tiếp → caption nằm trên ảnh; nhiều ảnh rỗng caption liền nhau → album 1 lần gửi). Mặc định **2000ms**, đặt 0 để tắt. Cấu hình per-rule trong form (cột *Gom cụm (ms)*).
- **↩ Xử lý REPLY (tin trả lời):** bridge đã bắt sẵn block `quote` (`ts` = **giờ tin gốc**, `globalMsgId` = id tin gốc, `msg`/`attach` = nội dung gốc). `reply_mode` per-rule:
  - `text` (**mặc định, Mức 1**): **gộp dòng trích dẫn vào chính tin gửi** (1 tin duy nhất): `🚀 prefix + ↩ <hh:mm:ss nếu bật giờ>: <nội dung 120c>\n<thân tin>` (**KHÔNG kèm tên người gửi**). Nếu tin gốc **khác ngày hiện tại** → tự thêm **ngày** (`↩ dd/mm/YYYY hh:mm:ss: …`). Với ảnh → trích dẫn nhập vào **caption** ảnh. `reply_quote_ms=1` để kèm **giờ tin gốc**.
  - `quote` (Mức 2): **quote native** Zalo khi parent map được sang tin đích (tra `forward_log.src_msg_id`→`dst_msg_id`); thiếu thì **tự lùi về text**. (Đã xác nhận API cần `cliMsgId`+`propertyExt`.)
  - `skip`: bỏ hẳn tin reply.
  - Cấu hình ở form tạo/sửa rule: **Xử lý reply** + checkbox **Giờ tin gốc trong trích dẫn (tự thêm ngày nếu tin cũ)**.
- **🧪 Debug send:** `POST /autobot/bot/send` thêm `reply_prefix` + `quote_json` (kèm `parts_json`, `dry`) → trả `preview` + `reply` + `quote` để verify.
- **📤 Gửi đa loại:** `buildBlocks` map parts → block gửi: `image`(1..N urls + caption), `video` (sendVideo + thumbnail + duration), `file` (tải về + đính kèm), `voice`, `sticker`, `link`. Prefix chỉ gắn **block đầu**. Lỗi tải/gửi → **fallback text** (không mất tin).
- **🔎 Kiểm chứng nhanh:** `POST /autobot/bot/send` (parts_json, dry=1) trả `preview` các block đã dựng (type/caption/urls) — verify không cần đọc nhóm đích.
- **🔒 Chỉ nghe nhóm đã add (allowlist)**: bot **chỉ LISTEN đúng các nhóm A của pipeline đang BẬT** (mỗi account). Mọi nhóm khác bị **bỏ qua ngay ở tầng listener** — không đọc, không dump, không vào controller. Nguồn allowlist = `src_group_id` của mọi rule `enabled`; truyền xuống bridge qua `--watch-groups only:<ids>` và refresh runtime bằng lệnh `set-watch` (không cần restart). Thêm/sửa/xoá/bật/tắt rule → tự đồng bộ; có nút **🔄 Đồng bộ nhóm nghe** + endpoint `POST /autobot/bot/watch`. Bridge đọc `relay.watchFrom/watchGroup`; `ping`/`ready` trả về `watch {mode,count}`. (Fallback an toàn: nếu account chưa có rule enabled nào → `all`.)
- Khi có **tin mới** ở A (do người khác đăng), bot **đăng lại** nội dung sang B. Tin do chính bot đăng **không** đẩy tiếp (tránh vòng lặp).
- **KHÔNG auto-reply** — bot chỉ chuyển tiếp, không tự trả lời ai.
- Mỗi luồng có: **tiền tố**, **hạn mức/giờ**, **giãn cách giây**, **DRY-RUN** (mặc định BẬT → chỉ ghi log), tuỳ chọn **đẩy cả tin bot**, **bật/tắt**.
- Thêm / **✏️ Sửa** / **🗑 Xoá** luồng; nhật ký chuyển tiếp realtime; nút **Bật/Tắt bot** per-tài-khoản.
- Bot tự chạy lại khi panel khởi động (nếu còn luồng BẬT). **Killswitch** dừng cả autobot.
- ⚠️ Chỉ bật DRY=OFF khi đã test kỹ; nên dùng nick phụ.

## API nhanh (bridge)

```bash
node bridge/zalo.mjs login-qr --account nick1 --creds-dir data/accounts --emit data/accounts/nick1.qr.log
node bridge/zalo.mjs whoami   --account nick1 --creds-dir data/accounts --json
node bridge/zalo.mjs send     --account nick1 --thread <id> --type group --text "hi" --dry-run --json
node bridge/zalo.mjs send     --account nick1 --thread <id> --blocks '[{"type":"text","content":"Chào {name}"},{"type":"image","src":"/a.jpg","caption":"ưu đãi"},{"type":"link","url":"https://x.vn"}]' --json
node bridge/zalo.mjs find-users --account nick1 --phones 0901111111,0902222222 --json
# Autobot (LISTEN + đẩy tin A->B; in NDJSON: ready / msg / reply)
node bridge/autobot.mjs --account nick1 --creds-dir data/accounts   # stdin: {"cmd":"ping|switch-to|send|stop"}
node bridge/zalo.mjs groups   --account nick1 --creds-dir data/accounts --json
node bridge/zalo.mjs group-members --account nick1 --group <groupId> --creds-dir data/accounts --json
```

## An toàn (BẮT BUỘC)

- Rate-limit gửi: 30 tin/giờ, 200 tin/ngày, 20 kết bạn/ngày, gap 25s + jitter 15s.
- **Rate-limit QUÉT NHÓM:** tối đa **20 lượt/giờ**, **60 lượt/ngày**, **gap ~55s** giữa các lượt (chống khóa nick).
  Đồng bộ danh sách nhóm: tối đa 1 lần/10 phút. Vượt giới hạn → webapp chặn và báo "chờ Ns".
- Warm-up nick mới: tăng dần, không blast.
- Mọi test code chạm side-effect **phải** ép dry-run (bài học fb-ops WF01 2026-09-25).
- Backup credentials trước thao tác xóa/đăng nhập lại.

### Giới hạn kỹ thuật của Zalo (quan trọng)

- **Quét thành viên nhóm:** Zalo KHÔNG trả full danh sách cho nhóm lớn. zca-js đọc `memVerList`; nhóm vừa/nhỏ (< ~200) thường trả **đủ**, nhóm lớn (500–1000) chỉ trả **một phần**.
  Webapp hiển thị rõ "đủ" / "một phần". Không có cách vòng qua trong API hiện tại (zca-js 2.2.0).

### Composer đa khối (P0 — 02/10)
- **Nội dung dạng khối có thứ tự** (`campaigns.blocks` JSON): Chữ / Ảnh / Link, gửi tuần tự; ảnh liền nhau → 1 album, mỗi ảnh có chú thích riêng; kéo ▲▼ / xoá; 👁 Xem thử nam/nữ.
- **Tương thích ngược 100%**: campaign cũ (body+album) tự dựng lại thành khối khi đọc; `from-file`/`from-group` vẫn nhận form cũ.
- Endpoint mới: `POST /upload/image` (1 ảnh → src+url), `POST /preview-blocks` (render list). Bridge thêm `--blocks '<json>'` (gửi theo thứ tự).
- Test hồi quy: `scripts/test_blocks.py` (14 case). **Backup full trước khi làm**: `backups/zaloclaw-studio-full-20261002-020616{,.tgz}`.

### Pipeline G2G — chuyển tiếp A→B (02/10)
- **Thuần LISTEN + đẩy tin**, KHÔNG auto-reply. Long-lived worker `bridge/autobot.mjs` mở WebSocket listener (`api.listener`, zca-js), stream tin mới NDJSON; panel giữ tiến trình + gửi lệnh qua stdin.
- Bảng `forward_rules` + `forward_log`; luồng có tiền tố / hạn mức-giờ / giãn cách / DRY / include_self / bật-tắt. Chống vòng lặp (bỏ tin `isSelf`), dedup `msgId`, chỉ xử lý nhóm (bỏ chat 1:1).

### Pipeline G2G — Miss tin ở “<NHOM_NGUON_A>” (02/10, đợt 8)
- **Triệu chứng**: rule `f_b4cb6bc3` (src `<SRC_GROUP_ID>` <NHOM_NGUON_A> → dst <NHOM_DICH>) miss tin mới nhất; capture chỉ thấy nhóm khác, 0 tin từ <NHOM_NGUON_A>.
- **Chẩn đoán**: mốc forward cuối của rule là **12:00:37**; listener **chết 12:15:52 (NORMAL_CLOSURE) → sống lại 13:10:23** ⇒ tin mới rơi vào **cửa sổ mù ~55 phút** (đã fix ở đợt 7, nhưng tin đã mất). <NHOM_NGUON_A> gửi **tần suất thấp** (~1 giờ/lần) nên rất dễ trúng cửa sổ chết.
- **Tác nhân phụ (đã loại)**: các listener **phụ** khi debug (`zcapture`/`_zlisten`/`_allcapture`) mở **cùng account nick1** ⇒ Zalo kick socket panel (`13:17:46 listen-disconnected`). Đã xoá hết; capture giờ nằm trong **chính bot panel** (một kết nối duy nhất).
- **Khắc phục**: `tick` health-monitor 120s⇒**60s**; thêm phát hiện **worker kẹt lỗi >10 phút** (không chỉ worker chết); log rõ `listen-reconnect`/`listen-reconnected` (trước bị nuốt, gây khó chẩn đoán).
- **Chờ xác minh**: cron `G2G-capture-watch` (mỗi 2 phút) sẽ **báo #zaloclaw** ngay khi bot nhận tin <NHOM_NGUON_A> kế tiếp (bằng chứng nhận được) hoặc có REPLY ở <NHOM_NGUON_B>.
- ⚠️ Tin đã miss **không khôi phục được** (history API 404) — chỉ đảm bảo các tin **từ nay** được nhận + tự hồi.

### Pipeline G2G — FIX listener tự chết không hồi (NORMAL_CLOSURE) (02/10, đợt 7)
- **Triệu chứng**: `AUTOBOT listen-disconnected :: nick1 :: NORMAL_CLOSURE` (12:15:52) rồi **im luôn** ~50 phút; không forward gì thêm dù panel vẫn báo `ready: true`. Healthz vẫn `ok` (panel không biết listener đã chết).
- **Gốc lỗi**: zca-js `start({retryOnClose:true})` **chỉ retry code nằm trong `features.socket.close_and_retry_codes`** (server cấu hình); code **1000 NORMAL_CLOSURE không nằm trong danh sách** → listener dừng vĩnh viễn, tiến trình vẫn sống. (Phụ trợ: các listener phụ `zcapture` mở **cùng tài khoản** gây Zalo xoay socket — đã bỏ.)
- **Fix 2 tầng**:
  - **Bridge (`bridge/supervise.mjs` mới)**: supervisor tự reconnect (`openAccount` lại) với backoff khi bắt `disconnected`/`closed`; emit `listen-reconnect`/`listen-reconnected`. `autobot.mjs` gắn supervisor + `noteActivity()` mỗi tin; `stop` gọi `SUP.stop()`.
  - **Panel (`webapp/core.py`)**: `_bot_health_monitor()` (thread daemon, tick 120s) — worker **chết** → `restart_bot`; worker **im >6h & queue rỗng** → refresh listener. `start_health_monitor()` (idempotent) gọi ở `app.py` startup. KHÔNG restart khi có việc trong queue (tránh cắt tin đang gửi).
- **Capture gộp 1 kết nối**: bot panel tự dump NDJSON (`ZS_RAW_DUMP=<root>/data/capture_raw.ndjson`) mọi tin nhóm kèm **block `quote` thô** (`hasQuote`, `ownerId`, `cliMsgId`, `msgType`, `attach`…). ⇒ không cần listener phụ (hết xung đột socket). Watcher `scripts/capture_watch.sh` lọc reply (`hasQuote:true`) **đúng nhóm mục tiêu**.
- **Bằng chứng end-to-end (đã chạy thật)**: giết worker → `13:09:56 stopped` → `13:10:22 health :: worker chết → restart` → `13:10:23 ready` (pid mới). Raw-dump thấy tin thật từ nhóm khác (nội dung người dùng).
- **Test**: `scripts/test_g2g_reconnect.py` (11 case) + `bridge/supervise.mjs` unit (9 case) + hồi quy 6 suite — ALL PASS. DB integrity ok.

### Pipeline G2G — FIX `Missing imageMetadataGetter` khi gửi ảnh (02/10, đợt 6)
- **Triệu chứng**: tin ảnh ở nhóm A → log lỗi `Missing \`imageMetadataGetter\`. Please provide it in the Zalo object options.` (class `ZaloApiMissingImageMetadataGetter`), ảnh không gửi được.
- **Gốc lỗi**: `bridge/autobot.mjs` tạo `new Zalo({ selfListen, logging, checkUpdate })` **thiếu** `imageMetadataGetter`; zca-js cần getter này để đọc kích thước ảnh trước khi upload (`dist/utils.js:303-325` ném lỗi nếu thiếu). `bridge/zalo.mjs` (campaign sender) **đã có** getter này — nên campaign gửi ảnh OK, còn G2G thì vỡ.
- **Fix**: thêm `imageMetadataGetter` (đọc file + `imageSize()` từ `image-size`) vào `zaloOptions()` của `autobot.mjs`; import `image-size`. Trả `{width,height,size}` hoặc `null` (file lỗi) — cùng chuẩn với `zalo.mjs`.
- **Test**: `scripts/test_g2g_media_bridge.mjs` (11 case: presence + hoạt động của getter trên PNG thật, `null` khi thiếu file, `sendBlocks` upload file tồn tại + giữ caption, URL chết xử lý êm) — ALL PASS. Hồi quy `test_g2g_media.py`/`test_g2g_dedup`/`test_campaign_fixes`/`test_autobot`/`test_blocks` — ALL PASS.

### Pipeline G2G — chuyển tiếp ẢNH (media) A→B (02/10, đợt 5)
- **Gốc lỗi**: tin ảnh trong nhóm A bị bỏ ảnh, chỉ text sang nhóm B. Vì (a) bridge lấy `content` dạng OBJECT → biến thành `title||description||JSON.stringify` (chuỗi rác, **không có URL ảnh**); (b) lệnh `send` của bridge chỉ nhận `text`, controller cũng chỉ đẩy `{text}` → ảnh không bao giờ được gửi; (c) ảnh *không caption* bị chặn ở `if not text` (coi như tin rỗng).
- **Fix bridge** (`bridge/relay.mjs` mới + `autobot.mjs`):
  - `pickMediaUrl(content)` rút URL http(s) (ưu tiên `oriUrl/hdUrl/rawUrl/normalUrl` rồi mới `thumb`); `classifyMedia(msgType,content)` phân loại ảnh/video.
  - Event `msg` emit thêm `mediaKind` + `media:[url]`; nhãn text của tin media = **caption** (không rơi về JSON dump).
  - Lệnh `send` nhận `blocks` `[{type:'image',urls,caption},{type:'text',content},{type:'link',url,caption}]`; ảnh được **tải về file tạm** (`downloadToTmp`, cache 200, UA Mozilla) rồi re-upload qua `api.sendMessage({attachments})` — đúng như luồng campaign.
- **Fix controller** (`webapp/core.py`): `_bot_handle_message` nhận `media`/`mediaKind` (tin ảnh-không-caption KHÔNG còn bị skip); `_bot_enqueue` mang `media` vào queue; `_fwd_send_cmd()` dựng `blocks` khi có media (1 ảnh + text ⇒ **1 message ảnh kèm caption**, khớp hành vi Zalo), không media ⇒ đường text cũ.
- Tin ảnh **kèm text** → ảnh + caption (1 msg); **không text** → chỉ ảnh. Link video (`chat.video.msg`) hiện gửi dạng ảnh-thumb; hỗ trợ video-file là việc tiếp theo.
- Test: `scripts/test_g2g_media.py` (12 case) + `bridge/relay.mjs` unit (HTTP mock) — ALL PASS; hồi quy `test_g2g_dedup/test_campaign_fixes/test_autobot/test_blocks` — ALL PASS.

### Pipeline G2G UI — fix tìm nhóm / dry-run / nút Sửa (02/10, đợt 4)
- **Tìm nhóm theo tên**: thêm ô `🔍 tìm nhóm…` (class `.ab-search`) phía trên mỗi `<select>` nhóm A/B — gõ để lọc option theo tên (hỗ trợ cả form tạo và form sửa).
- **DRY-RUN sai khi bỏ tick**: form thiếu hidden `dry_run=off` nên `_is_dry("")` mặc định = DRY. Thêm hidden `off` + checkbox `value=on` cho `dry_run`/`include_self`/`enabled`; thêm helper `_last_bool()` (lấy giá trị cuối khi htmx nối `"0,1"`).
- **Nút Sửa mất sau ~6s**: do auto-refresh `#rules-box` (every 6s) swap cả partial. Thêm `abBusy()` + chặn `htmx:beforeRequest` khi có dòng đang sửa/đang focus; `abEdit` chỉ mở 1 dòng; `abSaved` đóng + refresh sau khi lưu.
- Test: `_is_dry`/`_last_bool` unit + endpoint `autobot_new/update` (dry off→0, off,on→1) + render template — PASS; hồi quy 4 suite ALL PASS.

### Campaign engine — harden F1–F6 (02/10, đợt 3)
- **F1 (race double-send)**: `start_campaign` claim nguyên tử `_claim_target()` trước khi enqueue; `enqueue()` idempotent theo `target_id`. Hai click “Chạy” đồng thời không còn đẩy trùng 1 người.
- **F2 (`add_targets` đếm sai)**: chỉ `n += 1` khi `rowcount` (bỏ qua ON CONFLICT).
- **F3 (không chuẩn hoá SĐT)**: `db.canon_phone()`/`canon_ident()`; `add_targets` + `_parse_phones` chuẩn hoá → `0912…`/`+84912…`/`84912…` gộp 1 target; cột `targets.ident` + backfill.
- **F4 (trùng xuyên campaign)**: `db.sent_ident_recent()` chặn gửi lại 1 người trong `dedupe_sent_days` (mặc định 3) — chỉ áp cho LIVE, bỏ qua nhóm.
- **F5 (sleep giữ lock)**: bỏ mọi `time.sleep` trong worker/khóa. Job hoãn dùng scheduler riêng (`zs-sched`, `_delayed` + `_next_attempt`), send chỉ giữ `_send_lock`. Một chiến dịch bị throttle không còn treo các chiến dịch khác.
- **F6 (target kẹt pending)**: `_reconcile_queue()` + `_maybe_finish()` tự revive target stranded (không claim/queue/backoff).
- Test: `scripts/test_campaign_fixes.py` (8 case) — ALL PASS; hồi quy `test_g2g_dedup.py`, `test_autobot.py`, `test_blocks.py` — ALL PASS.

### Pipeline G2G — harden chống trùng (02/10, đợt 2)
- **L1 (dedup bền)**: thêm bảng `forward_seen(account_id,group_id,msg_key)` + `db.seen_forward()` — check-and-set **atomic**, sống qua restart. `_bot_dedup_ok` giờ 2 tầng: cache RAM (nhanh) + ledger DB (bền) ⇒ tin replay khi listener reconnect **không** bị đẩy lại.
- **L2 (khoá DB)**: `forward_log` thêm cột `src_msg_id` + **unique index** `ux_fwd_log(rule_id,src_group_id,src_msg_id) WHERE src_msg_id<>''`; `add_forward_log()` dùng `ON CONFLICT DO NOTHING` và trả `bool` — tầng ghi tự chặn trùng.
- **L3 (thiếu msgId)**: `_fwd_msg_key()` fallback hash `sha1(uidFrom|ts|text)` khi event không có id ⇒ tin thiếu id vẫn được dedup; log `AUTOBOT no-msgid` khi tỉ lệ cao.
- **L4**: dùng `realMsgId` (bridge emit thêm) làm khoá khi `msgId` rỗng.
- Test: `scripts/test_g2g_dedup.py` (6 case L1–L4) + hồi quy `test_autobot.py` (12), `test_blocks.py` — ALL PASS.
- Endpoints: `/autobot/new|{id}/update|{id}/delete|{id}/toggle`, `/autobot/bot/start|stop|restart`, `/ui/autobot[/log|/status]`. Tab **🤖 Autobot** có thêm/sửa/xoá luồng + chọn nhóm A/B + nhật ký realtime.
- Auto re-arm khi startup (`ensure_bots`). Killswitch chặn autobot. Test: `scripts/test_autobot.py` (12 case + integration).

### Sự cố "chiến dịch kẹt 'running', target đứng 'pending'" (03/10) — DEADLOCK _send_lock
- **Triệu chứng**: bấm **Chạy** → `queued 1/1` nhưng target **mãi `pending`**, campaign mãi `running`, **không spawn bridge `send`**, không có `SENT/FAIL`. `resume`/`start` lại đều trả `resumed:true, queued:0` (vì target bị coi là "đang chạy").
- **Gốc lỗi**: `_process_job` bọc `_do_send_job(...)` trong `with _send_lock:`, **và** `_do_send_job` cũng `with _send_lock: bridge_recv(...)`. `_send_lock = threading.Lock()` (**KHÔNG reentrant**) → cùng 1 thread acquire lần 2 ⇒ **deadlock vĩnh viễn**. Chẩn đoán bằng stack dump (`kill -USR1`, faulthandler) → thấy `zs-worker` kẹt tại `core.py:_do_send_job` dòng `with _send_lock`.
- **Khắc phục**: bỏ acquire lồng — **`_process_job` giữ `_send_lock` bao trọn** `_do_send_job`; trong `_do_send_job` gọi thẳng `bridge_recv(*args)`.
- **Test chống tái phát**: `scripts/test_send_no_deadlock.py` (dùng **DB tạm cô lập** — tuyệt đối không đụng `data/zs.db`) — chạy `_process_job` trong thread, phải trả về trong 10s.
- **Chẩn đoán về sau**: `kill -USR1 <pid panel>` → dump stack mọi thread ra `logs/panel-systemd.err` (đã bật `faulthandler` trong `app.py`).

### ⏰ Giờ im lặng — không gửi tin / kết bạn (03/10)
- Tab **Cài đặt** → mục **Giờ im lặng**: bật + chọn **Từ** / **Đến** (input `time`, giờ local). Trong khung giờ này engine **KHÔNG gửi tin nhắn và KHÔNG gửi lời mời kết bạn** — job được **park** (không ngủ, không đốt hạn mức) và **tự chạy tiếp** khi hết giờ.
- Khung giờ **qua nửa đêm** OK (VD `22:00 → 06:00`). `from == to` ⇒ coi như **tắt** (không bao giờ im lặng 24/7). Đầu/cuối khung là **exclusive**.
- Header hiện badge **🌙 Giờ im lặng → HH:MM** khi đang trong khung. Áp dụng cho **chiến dịch** (gửi tin + kết bạn); **DRY-RUN không bị chặn** (không có traffic thật).
- Lưu ở `settings.defaults` (`quiet_hours_enabled` / `quiet_from` / `quiet_to`). Code: `core.quiet_window/in_quiet_hours/quiet_seconds_left/quiet_status` + gate trong `_do_send_job`. Test: `scripts/test_quiet_hours.py` (32 case).

### Sự cố "Internal Server Error" toàn panel (03/10) — RÒ RỈ FILE-DESCRIPTOR
- **Triệu chứng**: mọi endpoint trả **500** (`sqlite3.OperationalError: unable to open database file`), kể cả `/healthz`; panel **vẫn `active`** (systemd không restart vì process sống).
- **Gốc lỗi (FD leak)**: `db.py::_conn()` trả connection dùng theo pattern `with _conn() as c:`. Trong `sqlite3`, `with conn:` **chỉ commit/rollback transaction**, **KHÔNG đóng connection** → mỗi truy vấn rò **1 connection = 2 FD** (db + WAL). Panel chạm trần **1024 FD** (504×`zs.db` + 501×`zs.db-wal`) → SQLite không mở nổi file → 500 toàn bộ.
- **Khắc phục**: `_conn()` chuyển thành **`@contextmanager`** — `yield` + `c.commit()` khi OK, `rollback()` khi lỗi, **`finally: c.close()`** luôn đóng. Thêm **`LimitNOFILE=16384`** cho service (headroom).
- **Test chống tái phát**: `scripts/test_db_no_fd_leak.py` — 400×2 read + 100 write ⇒ **delta FD = 0**.
- **Bài học**: mọi `with _conn()` phải là contextmanager tự-đóng; đừng tin `with sqlite3.connect() as c` là “đã đóng”.

### Bản vá an toàn (đợt P0/P1 — 01/10)
- **P0-1** Hạn mức chỉ tính tin **LIVE**: gửi DRY không còn ăn vào trần ngày/giờ (`count_sent_since`/`last_sent_ts` lọc `dry_run=0`).
- **P0-2** Toggle **DRY↔LIVE** giờ **re-bake** vào mọi job đang xếp hàng/đang chờ; bấm **Bật LIVE** có xác nhận.
- **P0-3** Nút **Chạy** trên chiến dịch LIVE có xác nhận; nhấn lại khi hết việc trả lỗi rõ (không gửi lặp); thêm **↻ Chạy lại lỗi** (`/campaigns/{id}/retry`).
- **P1-3** Chế độ **gửi-nhóm**: tự rút về **1 người nhận** (1 lần gửi vào nhóm) — chống spam N lần.
- **P1-4** Bắt đầu chiến dịch nhanh: chỉ tra đúng SĐT của campaign (không quét cả danh bạ) — ~2.6s thay vì ~10s.
- **P1-5** Cài đặt tần suất được **clamp** (không thể đặt 0/âm làm tê liệt chống khóa nick).
- **P1-2** Bỏ endpoint tài khoản rỗng chết; thêm tài khoản luôn kèm QR.

- [x] Bridge: login QR, whoami, send, find-users, friend-request, daemon (stdin/stdout)
- [x] Web: tài khoản + QR modal, tệp KH (import), chiến dịch (tạo/resolve/chạy/dừng/dry↔live), log, killswitch
- [x] **Quản lý tài khoản đầy đủ**: đổi tên nick · tắt/bật từng nick · 🔌 kiểm tra phiên (gắn `hết phiên`) · cảnh báo lệch creds · 🗑 xoá nick (tuỳ chọn xoá creds) — tất cả có audit log.
- [x] **Hạn mức theo nick trên UI**: tin/giờ · tin/ngày · kết bạn/ngày · quét/ngày (đã dùng/trần) + màu cảnh báo + thời điểm gửi/quét gần nhất.
- [x] **Chốt an toàn theo nick**: nick tắt / chưa đăng nhập không chạy LIVE, không quét nhóm (job giữ nguyên).
- [x] Engine: queue + rate-limit + jitter + pause + killswitch
- [x] **Cá nhân hoá tin nhắn theo tên + giới tính** ({name}, {salutation}→Anh/Chị, {gioi_tinh}) + preview
- [x] **Danh sách nhóm đang tham gia** + **quét thành viên nhóm** → xuất Tệp KH / tạo chiến dịch từ nhóm
  (đọc cả `memberIds` lẫn `memVerList`; rate-limit quét 20/giờ, 60/ngày, gap ~55s; hiển thị "đủ/một phần")
- [x] **Gửi nhiều ảnh (album) + text trong chiến dịch** — chọn nhiều ảnh ở form → album gửi trước, text gửi sau; lưu `data/media/`, thumbnail trên danh sách; `campaigns.images` (JSON).
- [x] **Composer ĐA KHỐI** (Chữ/Ảnh/Link, sắp xếp tự do, caption riêng) — xem mục “Composer đa khối (P0)”; `campaigns.blocks` + bridge `--blocks`.
- [ ] Group campaign (tạo nhóm / thêm thành viên / join link) — GĐ2
- [ ] Tag CRM nâng cao, report đã xem — GĐ2
- [ ] Multi-nick song song + proxy per account — GĐ3
- [ ] Cảnh báo sớm hết phiên (định kỳ) + nhắc đăng nhập lại — GĐ3
