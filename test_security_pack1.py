# -*- coding: utf-8 -*-
"""RED-тесты security-пакета 1.

Покрывает:
- rate-limit логина;
- cookie/session hardening и POST-only logout;
- валидацию column_id в add/edit/move;
- лимит backup upload;
- нейтрализацию Excel formula injection;
- запрет неизвестных SQLite triggers/views при restore.
"""
import os
import sqlite3
import tempfile
from io import BytesIO

TMP = tempfile.mkdtemp()
os.environ["HR_DATA_DIR"] = TMP
os.environ["HR_PASSWORD"] = "security-pass"
os.environ["HR_SECRET_KEY"] = "security-secret"

import app as appmod

app = appmod.app
app.config["TESTING"] = True
appmod.init_db()

failures = []


def check(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), "-", label, extra)
    if not cond:
        failures.append(label)


def open_db():
    con = sqlite3.connect(appmod.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


# --- 1. rate limit логина ---
attacker = app.test_client()
statuses = []
for _ in range(5):
    statuses.append(attacker.post(
        "/login", data={"password": "wrong"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
    ).status_code)
check("login: после серии ошибок включается rate-limit 429",
      statuses[-1] == 429, extra=str(statuses))
blocked_valid = attacker.post(
    "/login", data={"password": "security-pass"},
    environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
)
check("login: верный пароль не обходит активную блокировку", blocked_valid.status_code == 429)
# другой IP не блокируется общей блокировкой
other = app.test_client()
r = other.post(
    "/login", data={"password": "security-pass"},
    environ_overrides={"REMOTE_ADDR": "203.0.113.11"},
)
check("login: блокировка изолирована по IP", r.status_code == 302, extra=str(r.status_code))
con = open_db()
stored = con.execute(
    "SELECT COUNT(*) FROM login_failures WHERE ip='203.0.113.10'"
).fetchone()[0]
con.close()
check("login: rate-limit хранится в общей SQLite БД", stored == 5, extra=str(stored))

# --- подготовка авторизованного клиента + CSRF ---
c = app.test_client()
c.post("/login", data={"password": "security-pass"},
       environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
with c.session_transaction() as sess:
    sess["_csrf"] = "security-csrf"
CSRF = "security-csrf"


def post(url, data=None, **kw):
    d = dict(data or {})
    d.setdefault("_csrf", CSRF)
    return c.post(url, data=d, **kw)


# --- 2. cookie hardening + POST-only logout ---
check("session: HttpOnly включён", app.config.get("SESSION_COOKIE_HTTPONLY") is True)
check("session: SameSite задан", app.config.get("SESSION_COOKIE_SAMESITE") in ("Lax", "Strict"))
check("session: есть конечный срок жизни", bool(app.config.get("PERMANENT_SESSION_LIFETIME")))
r = c.get("/logout")
check("logout: GET не меняет состояние и не разрешён", r.status_code in (404, 405), extra=str(r.status_code))
r = post("/logout") if r.status_code in (404, 405) else None
check("logout: POST завершает сессию",
      r is not None and r.status_code == 302 and "/login" in (r.location or ""))
with c.session_transaction() as sess:
    check("logout: сессия очищена полностью", "authed" not in sess and "_csrf" not in sess)

# снова войти
c.post("/login", data={"password": "security-pass"})
with c.session_transaction() as sess:
    sess["_csrf"] = CSRF

# --- 3. column_id validation ---
post("/board/column/add", {"name": "Valid column"})
con = open_db()
cid = con.execute("SELECT id FROM kb_columns WHERE name='Valid column'").fetchone()["id"]
con.close()
r = post("/board/task/add", {"title": "bad add", "column_id": "999999"})
con = open_db()
add_row = con.execute("SELECT column_id FROM kb_tasks WHERE title='bad add'").fetchone()
con.close()
check("kanban add: несуществующий column_id отклонён без создания задачи",
      r.status_code == 400 and add_row is None,
      extra="status=%s row=%r" % (r.status_code, add_row))
post("/board/task/add", {"title": "valid task", "column_id": str(cid)})
con = open_db()
tid = con.execute("SELECT id FROM kb_tasks WHERE title='valid task'").fetchone()["id"]
con.close()
try:
    r = post(f"/board/task/{tid}/move", {"column_id": "999999"})
    move_status = r.status_code
except sqlite3.IntegrityError:
    move_status = 500
con = open_db()
after = con.execute("SELECT column_id FROM kb_tasks WHERE id=?", (tid,)).fetchone()["column_id"]
con.close()
check("kanban move: несуществующий column_id даёт 400 и не меняет задачу",
      move_status == 400 and after == cid,
      extra="status=%s column=%s" % (move_status, after))
try:
    r = post(f"/board/task/{tid}/edit", {
        "title": "valid task", "column_id": "999999",
        "description": "", "start_date": "", "due_date": "",
    })
    edit_status = r.status_code
except sqlite3.IntegrityError:
    edit_status = 500
check("kanban edit: несуществующий column_id отклонён", edit_status == 400,
      extra=str(edit_status))

# restore не должен принимать legacy/non-kanban колонку
con = open_db()
legacy = con.execute(
    "INSERT INTO kb_columns(name, kind, sort_order) VALUES ('legacy', 'legacy', 999)"
).lastrowid
con.execute("UPDATE kb_tasks SET column_id=?, archived_at=datetime('now') WHERE id=?",
            (legacy, tid))
con.commit(); con.close()
r = post(f"/board/task/{tid}/restore")
con = open_db()
restored_col = con.execute("SELECT column_id FROM kb_tasks WHERE id=?", (tid,)).fetchone()[0]
restored_kind = con.execute("SELECT kind FROM kb_columns WHERE id=?", (restored_col,)).fetchone()[0]
con.close()
check("kanban restore: legacy-колонка заменяется обычной", r.status_code == 302 and restored_kind == "kanban")

# --- 4. upload limit ---
limit = app.config.get("MAX_CONTENT_LENGTH")
check("backup import: MAX_CONTENT_LENGTH настроен", isinstance(limit, int) and limit > 0,
      extra=repr(limit))
if isinstance(limit, int) and limit > 0:
    old_limit = app.config["MAX_CONTENT_LENGTH"]
    app.config["MAX_CONTENT_LENGTH"] = 1024
    with c.session_transaction() as sess:
        sess["_csrf"] = CSRF
    r = c.post("/backup/import",
               data={"_csrf": CSRF,
                     "dbfile": (BytesIO(b"x" * 4096), "huge.db")},
               content_type="multipart/form-data")
    app.config["MAX_CONTENT_LENGTH"] = old_limit
    check("backup import: oversized upload отклонён 413", r.status_code == 413,
          extra=str(r.status_code))

# --- 5. Excel formula injection helper ---
formula_cases = ["=1+1", "+SUM(A1:A2)", "-1+2", "@cmd", "  =HYPERLINK('x')"]
safe_fn = getattr(appmod, "_xlsx_safe", None)
check("xlsx: sanitizer существует", callable(safe_fn))
if callable(safe_fn):
    for val in formula_cases:
        safe = safe_fn(val)
        check("xlsx: нейтрализовано %r" % val,
              isinstance(safe, str) and safe.lstrip().startswith("'"))
    check("xlsx: обычный текст не меняется", safe_fn("Иванов") == "Иванов")

# --- 6. restore rejects unknown trigger/view ---
con = open_db()
clean_path = os.path.join(TMP, "restore_trigger.db")
dst = sqlite3.connect(clean_path)
con.backup(dst)
dst.execute("CREATE TABLE audit_extra(id INTEGER)")
dst.execute("CREATE TRIGGER evil_trigger AFTER INSERT ON employees BEGIN DELETE FROM employees; END")
dst.commit(); dst.close(); con.close()
with open(clean_path, "rb") as fh:
    payload = fh.read()
r = post("/backup/import", {"dbfile": (BytesIO(payload), "trigger.db")},
         content_type="multipart/form-data", follow_redirects=True)
html = r.get_data(as_text=True).lower()
check("restore: неизвестные trigger/table отклоняются",
      "не удалось" in html or "невалид" in html or "не похож" in html)

print()
if failures:
    print("ИТОГ: ПРОВАЛЫ ->", failures)
    raise SystemExit(1)
print("ИТОГ: SECURITY PACK 1 OK")

# Не оставляем HR_DATA_DIR в окружении следующих standalone-тестов,
# если скрипты запускаются одной shell-командой.
os.environ.pop("HR_DATA_DIR", None)
