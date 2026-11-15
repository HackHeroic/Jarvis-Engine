#!/usr/bin/env bash
# Demo-day reset: run once on the morning of each demo, from the Jarvis-Engine folder.
#
#   bash scripts/demo_day.sh
#
# It reseeds the demo user as of today (three weeks of history ending yesterday, today's
# plan, the rule check-ins due), starts the backend with Claude first, uploads the demo
# syllabus so tasks link to it, and asks for today's morning brief.
#
# Every backend pointed at the shared database must be stopped first, teammates' too:
# a running backend's slot sweep works on the real clock and would set half-seeded
# tasks aside. This script can only check the one on this machine.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"
BASE="http://localhost:${PORT}"
API="${BASE}/api/v1"
SYLLABUS="docs/capstone/demo/DBMS-Syllabus-CS304.pdf"
LOG="${TMPDIR:-/tmp}/jarvis-demo-backend.log"

if curl -s -m 3 -o /dev/null "${BASE}/health"; then
  echo "A backend is running on port ${PORT}. Stop it (Ctrl+C in its terminal), then run this again."
  exit 1
fi

echo "1/4 Reseeding the demo user as of today..."
.venv/bin/python scripts/seed_demo_user.py --reset --base-url "${BASE}"

echo "2/4 Starting the backend (log: ${LOG})..."
CLAUDE_PRIMARY=true nohup .venv/bin/uvicorn app.main:app --port "${PORT}" > "${LOG}" 2>&1 &
echo "    backend pid $!"
for _ in $(seq 1 90); do
  curl -s -m 2 -o /dev/null "${BASE}/health" && break
  sleep 2
done
curl -s -m 5 -o /dev/null "${BASE}/health" || { echo "The backend did not start; read ${LOG}."; exit 1; }

echo "3/4 Uploading the demo syllabus..."
BODY="$(mktemp)"
trap 'rm -f "${BODY}"' EXIT
.venv/bin/python - "${SYLLABUS}" > "${BODY}" <<'PY'
import base64, json, pathlib, sys
path = pathlib.Path(sys.argv[1])
print(json.dumps({
    "file_base64": base64.b64encode(path.read_bytes()).decode(),
    "media_type": "pdf",
    "file_name": path.name,
    "user_id": "demo",
}))
PY
curl -s -m 300 -o /dev/null -w "    upload: HTTP %{http_code}\n" -X POST "${API}/ingestion/process" \
  -H "Content-Type: application/json" --data-binary "@${BODY}"

echo "4/4 Asking for today's brief..."
curl -s -m 120 -o /dev/null -w "    brief: HTTP %{http_code}\n" -X POST "${API}/anticipation/run?user_id=demo" \
  -H "Content-Type: application/json" -d '{"trigger":"morning_brief"}'

echo "Ready. Start the frontend (npm run dev in jarvis-frontend) and open http://localhost:3000/dashboard"
