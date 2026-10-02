#!/bin/sh
if [ -z "$HR_PASSWORD" ]; then
  HR_PASSWORD=$(head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 14)
  echo "============================================================"
  echo "[TeamBook] Переменная HR_PASSWORD не задана."
  echo "[TeamBook] Сгенерирован пароль для входа: $HR_PASSWORD"
  echo "[TeamBook] Сохраните и установите HR_PASSWORD, иначе пароль"
  echo "[TeamBook] сменится после пересоздания контейнера."
  echo "============================================================"
  export HR_PASSWORD
fi

rm -f /app/data/.maintenance.lock
if [ -f /app/data/.restore-fatal ]; then
  echo "[TeamBook] FATAL restore state; требуется ручное восстановление" >&2
  exit 76
fi

# Pending restore разбирается до миграций и запуска любых writers.
if [ -f /app/data/.restore-request.json ]; then
  python /app/restore_offline.py
  RESTORE_STATUS=$?
  if [ "$RESTORE_STATUS" -eq 75 ]; then exit 75; fi
  if [ "$RESTORE_STATUS" -eq 76 ]; then exit 76; fi
fi

python -c "import app; app.init_db()" || exit 1
python /app/bot.py > /app/data/teambot_supervisor.log 2>&1 &
BOT_PID=$!
gunicorn --bind 0.0.0.0:${PORT:-5000} --workers ${WEB_CONCURRENCY:-2} wsgi:app &
WEB_PID=$!

stop_pid() {
  PID="$1"
  kill -TERM "$PID" 2>/dev/null || return 0
  I=0
  while kill -0 "$PID" 2>/dev/null && [ "$I" -lt 15 ]; do sleep 1; I=$((I+1)); done
  if kill -0 "$PID" 2>/dev/null; then kill -KILL "$PID" 2>/dev/null || true; fi
  wait "$PID" 2>/dev/null || true
}
shutdown() {
  stop_pid "$WEB_PID"
  stop_pid "$BOT_PID"
}
trap 'shutdown' TERM INT EXIT

while kill -0 "$WEB_PID" 2>/dev/null && kill -0 "$BOT_PID" 2>/dev/null; do
  if [ -f /app/data/.restore-request.json ]; then
    shutdown
    python /app/restore_offline.py
    STATUS=$?
    trap - TERM INT EXIT
    if [ "$STATUS" -eq 75 ]; then exit 75; fi
    if [ "$STATUS" -eq 76 ]; then exit 76; fi
    exit 1
  fi
  sleep 1
done

shutdown
trap - TERM INT EXIT
exit 1
