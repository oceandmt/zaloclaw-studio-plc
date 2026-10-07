#!/usr/bin/env bash
# Print NEW captured events since the last check, from the panel's own listener
# dump (data/capture_raw.ndjson):
#   - ANY new message in the <NHOM_NGUON_A> source group (proof the bot receives it)
#   - ANY new REPLY (hasQuote) in the <NHOM_NGUON_B> group
# Prints nothing if there is nothing new. Never opens a second listener.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MATRIX="<SRC_GROUP_ID>"        # <NHOM_NGUON_A> (forward rule source)
TINHIEU="<DST_GROUP_ID>"       # <NHOM_NGUON_B> (reply study)
NDJ="$ROOT/data/capture_raw.ndjson"
STATE="$ROOT/data/.capture_seen_lines"
mkdir -p "$ROOT/data"; touch "$NDJ" "$STATE"
total=$(wc -l <"$NDJ" | tr -d ' ')
seen=$(cat "$STATE" 2>/dev/null || echo 0); [ -z "$seen" ] && seen=0
if [ "$total" -le "$seen" ]; then exit 0; fi
tail -n +$((seen+1)) "$NDJ" | python3 -c "
import sys, json
MATRIX='$MATRIX'; TINHIEU='$TINHIEU'
for line in sys.stdin:
    try: d=json.loads(line)
    except Exception: continue
    if d.get('ev')!='msg': continue
    g=str(d.get('group') or '')
    if g==MATRIX:
        print('MATRIX|' + json.dumps(d, ensure_ascii=False))
    elif g==TINHIEU and d.get('hasQuote'):
        print('REPLY|' + json.dumps(d, ensure_ascii=False))
" || true
echo "$total" >"$STATE"
