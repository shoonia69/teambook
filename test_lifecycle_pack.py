# -*- coding: utf-8 -*-
"""RED-тесты пакета 2: SQLite и lifecycle процессов."""
import os
import tempfile

ROOT = os.path.dirname(__file__)
tmp = tempfile.mkdtemp()
os.environ["HR_DATA_DIR"] = tmp
os.environ["HR_PASSWORD"] = "lifecycle-pass"
os.environ["HR_SECRET_KEY"] = "lifecycle-secret"

import app

failures = []

def check(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), "-", label, extra)
    if not cond:
        failures.append(label)

app.init_db()
with app.app.app_context():
    db = app.get_db()
    mode = db.execute("PRAGMA journal_mode").fetchone()[0].lower()
    timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    check("web SQLite использует WAL", mode == "wal", mode)
    check("web SQLite имеет busy_timeout", timeout >= 5000, str(timeout))

with open(os.path.join(ROOT, "wsgi.py"), encoding="utf-8") as f:
    wsgi = f.read()
check("wsgi не запускает миграции в каждом worker", "init_db()" not in wsgi)

with open(os.path.join(ROOT, "entrypoint.sh"), encoding="utf-8") as f:
    entry = f.read()
check("entrypoint запускает миграции до процессов", "app.init_db()" in entry)
check("entrypoint имеет trap graceful shutdown", "trap 'shutdown'" in entry or 'trap "shutdown"' in entry)
check("entrypoint сохраняет PID supervisor", "BOT_PID=$!" in entry)
check("entrypoint отслеживает gunicorn", "kill -0 \"$WEB_PID\"" in entry)

with open(os.path.join(ROOT, "bot.py"), encoding="utf-8") as f:
    bot = f.read()
check("supervisor обрабатывает SIGTERM", "signal.SIGTERM" in bot)
check("supervisor закрывает logfile", "logfile.close()" in bot)
check("supervisor имеет exponential backoff", "BACKOFF_MAX" in bot and "backoff" in bot)
check("supervisor учитывает maintenance lock", "MAINTENANCE_LOCK" in bot)

with open(os.path.join(ROOT, "bot_worker.py"), encoding="utf-8") as f:
    worker = f.read()
check("bot SQLite имеет busy_timeout", "busy_timeout" in worker)

check("restore передаётся PID1 для offline activation", hasattr(app, "RESTORE_REQUEST"))
check("restore делает уникальную резервную копию", hasattr(app, "_unique_restore_backup_path"))
with open(os.path.join(ROOT, "restore_offline.py"), encoding="utf-8") as f:
    offline = f.read()
check("entrypoint запускает offline restore только после остановки процессов",
      "shutdown\n    python /app/restore_offline.py" in entry)
check("offline restore валидирует staged path", "NAME_RE.fullmatch" in offline and "os.lstat" in offline)
check("offline restore имеет rollback", "os.replace(backup, CURRENT)" in offline)
check("offline restore quarantine-ит сбой", "_quarantine" in offline)
check("offline restore повторяет полную schema validation", "_validate_db_schema" in offline)
check("offline restore удаляет sidecars до rollback", "_remove_sidecars(CURRENT)" in offline)
check("fatal restore блокирует startup", ".restore-fatal" in entry and 'exit 76' in entry)
check("offline restore fsync открывает БД writable", 'os.open(CURRENT, os.O_RDWR)' in offline)
check("offline restore fsync открывает backup writable", 'os.open(backup, os.O_RDWR)' in offline)
check("успешный rollback не помечается fatal", "fatal = swapped and not rollback_ok" in offline and "rollback_ok = True" in offline)

print()
if failures:
    print("ИТОГ: ПРОВАЛЫ ->", failures)
    raise SystemExit(1)
print("ИТОГ: LIFECYCLE PACK OK")
