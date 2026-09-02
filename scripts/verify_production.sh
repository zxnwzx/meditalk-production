#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TMP_DIR=$(mktemp -d)
PORT="${PRODUCTION_TEST_PORT:-18000}"
BASE_URL="http://127.0.0.1:${PORT}"
COOKIE_JAR="${TMP_DIR}/cookies.txt"
DB_PATH="${TMP_DIR}/meditalk-production-test.db"
UPLOAD_DIR="${TMP_DIR}/uploads"
mkdir -p "$UPLOAD_DIR"

cleanup() {
  [ -n "${WEB_PID:-}" ] && kill "$WEB_PID" 2>/dev/null || true
  [ -n "${SCHEDULER_PID:-}" ] && kill "$SCHEDULER_PID" 2>/dev/null || true
  wait "${WEB_PID:-}" 2>/dev/null || true
  wait "${SCHEDULER_PID:-}" 2>/dev/null || true
  if [ "${KEEP_TEST_ARTIFACTS:-0}" = "1" ]; then
    echo "Verification artifacts retained at: ${TMP_DIR}" >&2
  else
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT INT TERM

cd "$ROOT"
export SECRET_KEY="production-verification-only-secret-not-for-deployment"
export SESSION_COOKIE_SECURE=0
export PORT
export MEDITOK_DB_PATH="$DB_PATH"
export MEDITOK_UPLOAD_DIR="$UPLOAD_DIR"
export SCHEDULE_WORKER_ENABLED=1
export PUBLISH_POLL_SECONDS=1
unset DATABASE_URL REDIS_URL

gunicorn --config gunicorn.conf.py wsgi:app >"${TMP_DIR}/gunicorn.log" 2>&1 &
WEB_PID=$!

for _ in $(seq 1 20); do
  if curl -fsS "${BASE_URL}/healthz" >/dev/null; then break; fi
  sleep 1
done
curl -fsS "${BASE_URL}/healthz" | grep -q '"status":"ok"'
python scheduler.py >"${TMP_DIR}/scheduler.log" 2>&1 &
SCHEDULER_PID=$!

csrf_from() {
  sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' | head -n 1
}

LOGIN_TOKEN=$(curl -fsS -c "$COOKIE_JAR" "${BASE_URL}/login" | csrf_from)
[ -n "$LOGIN_TOKEN" ]
curl -fsS -b "$COOKIE_JAR" -c "$COOKIE_JAR" -X POST "${BASE_URL}/login" \
  --data-urlencode "username=editor" \
  --data-urlencode "password=editor1234" \
  --data-urlencode "csrf_token=${LOGIN_TOKEN}" \
  -o /dev/null -D "${TMP_DIR}/login.headers"
grep -q '/admin' "${TMP_DIR}/login.headers"

WRITE_TOKEN=$(curl -fsS -b "$COOKIE_JAR" -c "$COOKIE_JAR" "${BASE_URL}/admin/write" | csrf_from)
[ -n "$WRITE_TOKEN" ]
ARTICLE_TITLE="프로덕션 예약 발행 검증"
SCHEDULED_AT=$(TZ=Asia/Seoul date -d '+1 minute' '+%Y-%m-%dT%H:%M')
curl -fsS -b "$COOKIE_JAR" -c "$COOKIE_JAR" -X POST "${BASE_URL}/admin/write" \
  --data-urlencode "title=${ARTICLE_TITLE}" \
  --data-urlencode "category=임상시험" \
  --data-urlencode "deck=Gunicorn과 예약 발행 워커의 직접 검증입니다." \
  --data-urlencode "body=예약 시간이 도달하면 자동으로 공개되어야 합니다." \
  --data-urlencode "action=schedule" \
  --data-urlencode "scheduled_at=${SCHEDULED_AT}" \
  --data-urlencode "csrf_token=${WRITE_TOKEN}" \
  -o /dev/null -D "${TMP_DIR}/schedule.headers"
grep -q '/admin' "${TMP_DIR}/schedule.headers"

sleep 70
curl -fsS "${BASE_URL}/" >"${TMP_DIR}/home.html"
grep -q "$ARTICLE_TITLE" "${TMP_DIR}/home.html"
grep -q '예약 발행 자동 실행' "${TMP_DIR}/scheduler.log"

echo "PASS: Gunicorn health check, editor article creation, scheduled publishing, and public article rendering"
