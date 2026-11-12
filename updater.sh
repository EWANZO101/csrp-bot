#!/usr/bin/env bash
# Host-side updater. Runs every minute from cron (installed by install.sh).
# When the panel drops data/update-request.json it pulls the requested git ref and rebuilds the bot,
# writing progress to data/update-status.json (the panel shows it and reports it to the developer hub).
set -uo pipefail
DIR="${1:-$(cd "$(dirname "$0")" && pwd)}"
cd "$DIR" || exit 1
REQ=data/update-request.json
STATUS=data/update-status.json
touch data/.updater 2>/dev/null
[ -f "$REQ" ] || exit 0
exec 9>/tmp/csrp-bot-updater.lock; flock -n 9 || exit 0

json_get(){ sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" "$2" | head -n1; }
REF=$(json_get ref "$REQ"); REF=${REF:-main}
TARGET=$(json_get version "$REQ")
BY=$(json_get by "$REQ")
FROM=$(cat VERSION 2>/dev/null || echo "?")
mv "$REQ" data/update-request.processing
now(){ date -u +%Y-%m-%dT%H:%M:%SZ; }
write_status(){ # state ok log
  python3 - "$STATUS" "$1" "$FROM" "$(cat VERSION 2>/dev/null)" "$TARGET" "$BY" "$(now)" "$3" <<'PY'
import json,sys
f,state,frm,to,target,by,at,logf=sys.argv[1:9]
log=open(logf).read()[-6000:] if logf and logf!="-" else ""
json.dump({"state":state,"from":frm,"to":to,"target":target,"by":by,"at":at,"log":log},open(f,"w"))
PY
}
LOG=$(mktemp)
write_status running - -
(
  echo "== $(now) updating $FROM → $REF (${TARGET:-latest}) requested by ${BY:-panel}"
  if ! [[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]]; then echo "bad ref"; exit 3; fi
  git fetch --tags origin "$REF" || exit 2
  if [ "$REF" = "main" ]; then git checkout -q main && git reset -q --hard origin/main; else git checkout -q --detach FETCH_HEAD; fi
  echo "now at $(git rev-parse --short HEAD): $(git log -1 --pretty=%s)"
  docker compose up -d --build 2>&1 | tail -n 25
  exit "${PIPESTATUS[0]}"
) > "$LOG" 2>&1
RC=$?
if [ "$RC" -eq 0 ] && docker compose ps --status running --services 2>/dev/null | grep -q '^bot$'; then
  write_status done - "$LOG"
else
  write_status failed - "$LOG"
fi
rm -f data/update-request.processing "$LOG"
