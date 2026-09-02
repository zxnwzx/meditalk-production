#!/bin/sh
set -eu

if [ -z "${SECRET_KEY:-}" ] || [ "${SECRET_KEY}" = "meditok-dev-secret-change-in-production" ]; then
  echo "SECRET_KEY must be set to a long random value in production." >&2
  exit 1
fi

export SCHEDULE_WORKER_ENABLED=1
gunicorn --config gunicorn.conf.py wsgi:app &
WEB_PID=$!

# SQLite 초기화가 끝난 뒤 예약 워커를 시작해 첫 기동 시 DDL 경합을 피합니다.
READY=0
for _ in $(seq 1 20); do
  if python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/healthz', timeout=2).read()" 2>/dev/null; then
    READY=1
    break
  fi
  if ! kill -0 "$WEB_PID" 2>/dev/null; then
    wait "$WEB_PID" || true
    exit 1
  fi
  sleep 1
done
if [ "$READY" -ne 1 ]; then
  echo "Gunicorn did not become healthy in time." >&2
  kill -TERM "$WEB_PID" 2>/dev/null || true
  wait "$WEB_PID" 2>/dev/null || true
  exit 1
fi

python scheduler.py &
SCHEDULER_PID=$!

shutdown() {
  kill -TERM "$WEB_PID" "$SCHEDULER_PID" 2>/dev/null || true
  wait "$WEB_PID" 2>/dev/null || true
  wait "$SCHEDULER_PID" 2>/dev/null || true
  exit 0
}

trap shutdown INT TERM
wait "$WEB_PID"
STATUS=$?
kill -TERM "$SCHEDULER_PID" 2>/dev/null || true
wait "$SCHEDULER_PID" 2>/dev/null || true
exit "$STATUS"
