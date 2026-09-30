# -*- coding: utf-8 -*-
"""Регрессионные тесты правок по аудиту (версия coder-senior).

Отдельный процесс и временная БД. Проверяет веб-регрессии:
CSRF, счётчик «Доска» (только канбан), навигация «вперёд» в архиве,
делегирование с due_date, удаление столбца+восстановление (column_id),
commit автоочистки корзины, утечка temp-файла бэкапа, Excel/PDF при
небезопасном имени/комментарии, drop-мишень бэклога в todo, dragend в доске,
fail-closed пустой пароль, compare_digest с кириллицей, archive next.
"""
import os
import tempfile
import sys
import datetime
import sqlite3
from io import BytesIO

src_tmp = tempfile.mkdtemp()
os.environ["HR_DATA_DIR"] = src_tmp
os.environ["HR_PASSWORD"] = "audit-pass-1"

import app as appmod
import app

app = appmod.app
app.config["TESTING"] = True
appmod.init_db()

failures = []


def check(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), "-", label, extra)
    if not cond:
        failures.append(label)


def dbraw(sql, args=()):
    conn = sqlite3.connect(os.path.join(src_tmp, "hr_notes.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def dbrawcommit(sql, args=()):
    conn = sqlite3.connect(os.path.join(src_tmp, "hr_notes.db"))
    conn.execute(sql, args)
    conn.commit()
    conn.close()


c = app.test_client()

# --- подготовка: логин, CSRF-токен, обёртка клиента ---
c.post("/login", data={"password": "audit-pass-1"}, follow_redirects=True)
with c.session_transaction() as s:
    s["_csrf"] = "audit-csrf-tok"
CSRF = "audit-csrf-tok"
_raw_post = c.post  # сырой клиент (без авто-инжекта) для негативных CSRF-проверок


def _wrap_post(url, *args, **kwargs):
    data = kwargs.get("data")
    if data is None:
        kwargs["data"] = {"_csrf": CSRF}
    elif isinstance(data, dict) and "_csrf" not in data:
        data = dict(data)
        data["_csrf"] = CSRF
        kwargs["data"] = data
    return _raw_post(url, *args, **kwargs)


c.post = _wrap_post


def post(url, data=None, **kw):
    data = dict(data or {})
    data.setdefault("_csrf", CSRF)
    return c.post(url, data=data, **kw)


# =====================================================================
# 1) CSRF: изменяющий POST без токена / с чужим Origin отклоняется
# =====================================================================
post("/board/column/add", data={"name": "Кол CSRF"})
r = _raw_post("/todo/add", data={"title": "CSRF-no-token"})
check("CSRF: POST без токена отклонён (400)", r.status_code == 400)
r = _raw_post("/todo/add", data={"title": "CSRF-wrong", "_csrf": "wrong"})
check("CSRF: POST с неверным токеном отклонён (400)", r.status_code == 400)
r = _raw_post("/todo/add", data={"title": "CSRF-foreign-origin", "_csrf": CSRF},
              headers={"Origin": "http://evil.example"})
check("CSRF: POST с чужим Origin отклонён (400)", r.status_code == 400)
# compare_digest сравнивает по байтам: не-ASCII токен не должен давать 500
r = _raw_post("/todo/add", data={"title": "CSRF-cyr", "_csrf": "кириллицатокен"})
check("CSRF: POST с кириллическим _csrf отклонён 400, не 500", r.status_code == 400,
      extra="status=%d" % r.status_code)
r = _raw_post("/todo/add", data={"title": "CSRF-cyr-hdr"},
              headers={"X-CSRF-Token": "кириллицатокен"})
check("CSRF: header с кириллическим токеном отклонён 400, не 500", r.status_code == 400,
      extra="status=%d" % r.status_code)
r = post("/todo/add", data={"title": "CSRF-ok"})
check("CSRF: POST корректный токен проходит",
      r.status_code in (200, 302) and len(dbraw("SELECT 1 FROM todo_items WHERE title='CSRF-ok'")) == 1)

# =====================================================================
# 2) Счётчик «Доска» считает ТОЛЬКО просроченные канбан-задачи (не todo)
# =====================================================================
import re as _re
# только просроченная todo-задача, канбан-просроченных нет
dbrawcommit("INSERT INTO todo_items (title, status, due_date) VALUES ('todo overdue', 'backlog', '2000-01-01')")
r = c.get("/")
h = r.get_data(as_text=True)
m = _re.search(r'Доска<span class="nav-badge">(\d+)</span>', h)
check("Доска: бейдж не считает просроченный todo (0/нет)",
      m is None, extra=("got=%s" % m.group(1) if m else "none"))
dbrawcommit("DELETE FROM todo_items WHERE title='todo overdue'")

# =====================================================================
# 3) Архив: навигация «вперёд» (next) на следующий день
# =====================================================================
yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
dbrawcommit("INSERT INTO todo_items (title, status, done_date) VALUES ('arc yest', 'done', ?)", (yesterday,))
r = c.get("/todo/archive?date=" + yesterday)
h = r.get_data(as_text=True)
# должна быть ссылка на следующий день (сегодня)
today = datetime.date.today().isoformat()
check("Архив: есть ссылка «вперёд» на следующий день",
      "Следующий день" in h and today in h,
      extra=("today=" + today))

# =====================================================================
# 4) Делегирование todo сохраняет due_date
# =====================================================================
dbrawcommit("INSERT INTO todo_items (title, status, due_date) VALUES ('дел с дедлайном', 'backlog', '2030-05-05')")
dd = dbraw("SELECT id FROM todo_items WHERE title='дел с дедлайном'")[0]["id"]
post(f"/todo/{dd}/delegate", follow_redirects=True)
kb2 = dbraw("SELECT due_date FROM kb_tasks WHERE title='дел с дедлайном'")
check("Делегирование: due_date переносится в канбан",
      len(kb2) == 1 and kb2[0]["due_date"] == "2030-05-05",
      extra=("due=" + str(kb2[0]["due_date"]) if kb2 else "none"))
dbrawcommit("DELETE FROM kb_tasks WHERE title='дел с дедлайном'")
dbrawcommit("DELETE FROM todo_items WHERE title='дел с дедлайном'")

# =====================================================================
# 5) Удаление столбца с только-в-корзине задачами + восстановление
# =====================================================================
post("/board/column/add", data={"name": "Кол Purge"})
col = dbraw("SELECT id FROM kb_columns WHERE name='Кол Purge'")[0]
post("/board/task/add", data={"title": "Карточка для колонны", "column_id": str(col["id"])})
tk = dbraw("SELECT id FROM kb_tasks WHERE title='Карточка для колонны'")[0]
c.post(f"/board/task/{tk['id']}/trash")  # в корзину
post(f"/board/column/{col['id']}/delete", follow_redirects=True)
c.post(f"/board/task/{tk['id']}/restore", follow_redirects=True)
row = dbraw("SELECT column_id FROM kb_tasks WHERE id=?", (tk["id"],))[0]
existing = dbraw("SELECT id FROM kb_columns WHERE kind='kanban'")
check("Корзина→удалить столбец→восстановить: column_id корректен",
      row["column_id"] is not None and any(x["id"] == row["column_id"] for x in existing),
      extra=("colid=" + str(row["column_id"])))
dbrawcommit("DELETE FROM kb_tasks WHERE id=?", (tk["id"],))

# =====================================================================
# 6) Автоочистка корзины (GET /board) фиксирует удаление (commit)
# =====================================================================
post("/board/column/add", data={"name": "Кол Auto Purge"})
col2 = dbraw("SELECT id FROM kb_columns WHERE name='Кол Auto Purge'")[0]
post("/board/task/add", data={"title": "Старый мусор", "column_id": str(col2["id"])})
tk2 = dbraw("SELECT id FROM kb_tasks WHERE title='Старый мусор'")[0]
old_ts = (datetime.datetime.now() - datetime.timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
dbrawcommit("UPDATE kb_tasks SET deleted_at=? WHERE id=?", (old_ts, tk2["id"]))
c.get("/board")  # должен запустить автоочистку
left = dbraw("SELECT COUNT(*) c FROM kb_tasks WHERE id=? AND deleted_at!=?", (tk2["id"], "not"))
check("Автоочистка корзины коммитится (GET удаляет старый мусор)",
      len(dbraw("SELECT 1 FROM kb_tasks WHERE id=?", (tk2["id"],))) == 0)
dbrawcommit("DELETE FROM kb_tasks WHERE id=?", (tk2["id"],))
post(f"/board/column/{col2['id']}/delete", follow_redirects=True)

# =====================================================================
# 7) Бэкап-экспорт не оставляет temp-файл
# =====================================================================
calls = {}
orig_mkstemp = appmod.tempfile.mkstemp


def fake_mkstemp(**kw):
    p = os.path.join(src_tmp, "probe_backup_%d.db" % len(calls))
    calls["path"] = p
    return (os.open(p, os.O_CREAT | os.O_RDWR), p)


appmod.tempfile.mkstemp = fake_mkstemp
try:
    r = c.get("/backup/export")
finally:
    appmod.tempfile.mkstemp = orig_mkstemp
if hasattr(r, "close"):
    r.close()  # триггерит call_on_close -> удаление temp-файла
p = calls.get("path")
check("Бэкап-экспорт: temp-файл удалён после ответа",
      r.status_code == 200 and (p is None or not os.path.exists(p)),
      extra=("exists=" + str(os.path.exists(p)) if p else "none"))

# =====================================================================
# 8) Excel/PDF устойчивы к '/' в имени и '<b>' в комментариях
# =====================================================================
r = post("/employee/new", data={"name": "Иван Сп/ециалист", "salary": "1"}, follow_redirects=True)
ee = dbraw("SELECT id FROM employees WHERE name='Иван Сп/ециалист'")[0]["id"]
dbrawcommit(
    "INSERT INTO year_records (employee_id, year, semester, comments, updated_at) "
    "VALUES (?,?,?,?, datetime('now'))", (ee, 2026, "1H", "комментарий с <b> незакрытым"))
def _safe_get(url):
    try:
        r = c.get(url)
        return (r.status_code, r.data[:4])
    except Exception as e:
        return (-1, str(e)[:20].encode())


# Excel с '/' в имени и PDF с незакрытым <b> — не должны падать
st, head = _safe_get(f"/employee/{ee}/report?year=2026&format=xlsx")
check("Excel: имя с '/' не ломает отчёт (200 PK)", st == 200 and head[:2] == b"PK",
      extra=("status=" + str(st) + " head=" + str(head[:2])))
st, head = _safe_get(f"/employee/{ee}/report?year=2026&format=pdf")
check("PDF: незакрытый <b> не ломает отчёт (200 %PDF)", st == 200 and head[:4] == b"%PDF",
      extra=("status=" + str(st)))
dbrawcommit("DELETE FROM year_records WHERE employee_id=?", (ee,))
dbrawcommit("DELETE FROM employees WHERE id=?", (ee,))

# =====================================================================
# 9) todo: бэклог — drop-мишень для возврата из квадранта (#18)
# =====================================================================
r = c.get("/todo")
h = r.get_data(as_text=True)
check("todo: у бэклога есть drop/ondrop (возврат из квадранта)",
      "todoDrop(event, 'backlog')" in h and "ondragover" in h and "ondrop=\"todoDrop(event, 'backlog')\"" in h)

# =====================================================================
# 10) Доска: dragend сбрасывает draggedId/draggedColId (#11)
# =====================================================================
r = c.get("/board")
h = r.get_data(as_text=True)
check("доска: карточка имеет ondragend", "ondragend=\"dragEnd(event)\"" in h)
check("доска: ручка столбца имеет ondragend", 'ondragstart="dragCol(event)" ondragend="dragEnd(event)"' in h)
check("доска: есть dragEnd(), сбрасывающий оба id",
      "function dragEnd" in h and "draggedId = null" in h and "draggedColId = null" in h)

# =====================================================================
# 11) fail-closed: пустой пароль не пускает + compare_digest кириллица
# =====================================================================
c.get("/logout")
appmod.ADMIN_PASSWORD = ""
c.post("/login", data={"password": ""}, follow_redirects=True)
r = c.get("/", follow_redirects=True)
check("fail-closed: пустой HR_PASSWORD не даёт войти пустой пароль",
      "Вход" in r.get_data(as_text=True) and "stats-row" not in r.get_data(as_text=True))
# кириллический пароль
appmod.ADMIN_PASSWORD = "пароль-123"
c.get("/logout")
r = c.post("/login", data={"password": "пароль-123"}, follow_redirects=True)
check("логин: кириллический пароль работает (compare_digest без TypeError)",
      r.status_code == 200 and "Сотрудники" in r.get_data(as_text=True))

print()
if failures:
    print(f"ИТОГ: {len(failures)} ПРОВАЛЕНО -> {failures}")
    sys.exit(1)
print("ИТОГ: ВСЕ ТЕСТЫ ПРОЙДЕНЫ")