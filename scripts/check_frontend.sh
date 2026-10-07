#!/usr/bin/env bash
# Frontend JS sanity check for zaloclaw-studio.
#
# Catches the two bugs that bit us in production:
#   1. Broken quoting in HTML event attributes (e.g. |tojson inside a
#      double-quoted onclick="..." -> attribute ends early -> SyntaxError).
#   2. Non-JS tokens inside <script> blocks (e.g. a stray Python `def foo(){`),
#      which makes the WHOLE block fail to parse and kills every function in it.
#
# Usage: scripts/check_frontend.sh [base_url]
# With no URL it checks the on-disk templates only.
set -u
cd "$(dirname "$0")/.."

PY=webapp/.venv/bin/python
[ -x "$PY" ] || PY=python3
command -v node >/dev/null || { echo "node not found"; exit 2; }

BASE="${1:-}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

"$PY" - "$BASE" "$TMP" <<'PY'
import glob, re, subprocess, sys, urllib.request
from html.parser import HTMLParser

base, tmp = sys.argv[1], sys.argv[2]
pages = []
if base:
    pages = ["/", "/ui/tab/accounts", "/ui/tab/contacts", "/ui/tab/groups",
             "/ui/tab/campaigns", "/ui/tab/logs", "/ui/tab/scans", "/ui/tab/settings"]

bad = 0

def js_check(code, label, body=True):
    global bad
    src = ("(function(event){\n" + code + "\n});") if body else code
    p = f"{tmp}/x.js"
    open(p, "w").write(src)
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
    if r.returncode != 0:
        bad += 1
        last = (r.stderr.strip().splitlines() or ["?"])[-1]
        print(f"  FAIL {label}: {last}")

# 1) inline <script> blocks in every template
for path in sorted(glob.glob("webapp/templates/*.html")):
    html = open(path, encoding="utf-8").read()
    for i, b in enumerate(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)):
        if b.strip():
            js_check(b, f"{path} <script#{i}>", body=False)
print("inline <script> blocks: checked")

# 2) event attributes from rendered pages (parsed exactly like a browser would)
class Grab(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.h = []
    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if v and (k.lower().startswith("on") or k.lower().startswith("hx-on")):
                self.h.append((tag, k, v))

n = 0
for pg in pages:
    try:
        html = urllib.request.urlopen(base + pg, timeout=10).read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  WARN fetch {pg}: {e}")
        continue
    g = Grab(); g.feed(html)
    for tag, attr, code in g.h:
        n += 1
        js_check(code, f"{pg} <{tag} {attr}>")
print(f"event attributes checked: {n}")

sys.exit(1 if bad else 0)
PY
rc=$?
[ $rc -eq 0 ] && echo "OK: all frontend JS valid" || echo "FAILED: invalid frontend JS found"
exit $rc
