# -*- coding: utf-8 -*-
"""Тесты восстановления/импорта и миграций (отдельный процесс/БД):
P1.4 — импортируемая старая БД мигрируется ДО активации (нет криша /todo);
      проверить успешную АКТИВАЦИЮ по уникальным маркерам записей, а не только /todo;
      ошибочный импорт отвергается, исходная БД сохраняется.
P1.5 — удаление legacy бэклога и emi% столбцов переносит ВСЕ связанные задачи
      (акт/архив/корзина), не ломая FK; emi-only база получает столбец-приёмник.
      После миграций обязательно PRAGMA foreign_key_check пуст (не только вставка).
"""
import os
import sqlite3
import tempfile
import sys
from io import BytesIO

tmp = tempfile.mkdtemp()
os.environ["HR_DATA_DIR"] = tmp
os.environ["HR_PASSWORD"] = "restore-pass"

import app as appmod
import restore_offline as restoremod

app = appmod.app
app.config["TESTING"] = True
appmod.init_db()
failures = []


def check(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), "-", label, extra)
    if not cond:
        failures.append(label)


def newconn():
    return appmod.get_db() if hasattr(appmod, "get_db") and False else _open(appmod.DB_PATH)


def _open(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def fk_check(db):
    return [tuple(r) for r in db.execute("PRAGMA foreign_key_check").fetchall()]


# =====================================================================
# A) Импорт «старой» БД (нет tag/due_date у todo_items) -> успешная активация
# по уникальным маркерам импортированных записей
# =====================================================================
MARK_EMP = "УникальныйСотрудникИмпорт"
MARK_TODO = "УникальнаяЗадачаИмпорт"
up_path = os.path.join(tmp, "upload_old.db")
u = sqlite3.connect(up_path)
u.executescript("""
CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE departments (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE employees (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL,
  department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
  salary TEXT DEFAULT '', notes TEXT DEFAULT '', hire_date TEXT DEFAULT '',
  active INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE todo_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'backlog', sort_order INTEGER NOT NULL DEFAULT 0,
  assigned_date TEXT DEFAULT '', done_date TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now'))
);
""")
u.execute("INSERT INTO employees (name) VALUES (?)", (MARK_EMP,))
u.execute("INSERT INTO todo_items (title, status) VALUES (?, 'backlog')", (MARK_TODO,))
u.commit()
u.close()

c = app.test_client()
c.post("/login", data={"password": "restore-pass"}, follow_redirects=True)
with c.session_transaction() as s:
    s["_csrf"] = "restore-csrf-tok"


def _wrap_rpost(url, *args, **kwargs):
    data = kwargs.get("data")
    if data is None:
        kwargs["data"] = {"_csrf": "restore-csrf-tok"}
    elif isinstance(data, dict) and "_csrf" not in data:
        data = dict(data)
        data["_csrf"] = "restore-csrf-tok"
        kwargs["data"] = data
    return c.post(url, *args, **kwargs)


def _safe_get(url):
    try:
        return c.get(url).status_code
    except Exception as e:
        return -1


def _activate_pending_restore():
    import json
    with open(appmod.RESTORE_REQUEST, encoding="utf-8") as fh:
        staged = json.load(fh)["staged"]
    os.chmod(staged, 0o600)
    restoremod.DATA = tmp
    restoremod.REQ = appmod.RESTORE_REQUEST
    restoremod.LOCK = os.path.join(tmp, ".maintenance.lock")
    restoremod.FATAL = os.path.join(tmp, ".restore-fatal")
    restoremod.CURRENT = appmod.DB_PATH
    restoremod.NAME_RE = __import__("re").compile(r"^\.restore-staged-[0-9a-f]{16}\.db$")
    return restoremod.activate()


# До импорта в активной БД маркера НЕТ (доказ. что проверка не ложноположительна)
db0 = _open(appmod.DB_PATH)
pre_names = [r["name"] for r in db0.execute("SELECT name FROM employees").fetchall()]
db0.close()
check("A: ДО импорта уникального маркера нет в активной БД", MARK_EMP not in pre_names,
      extra="count=%d" % len(pre_names))

with open(up_path, "rb") as f:
    data = f.read()
r = _wrap_rpost("/backup/import", data={"dbfile": (BytesIO(data), "old_restore.db")},
                content_type="multipart/form-data", follow_redirects=True)
activate_status = _activate_pending_restore()

db1 = _open(appmod.DB_PATH)
post_names = [r["name"] for r in db1.execute("SELECT name FROM employees").fetchall()]
todo_titles = [r["title"] for r in db1.execute(
    "SELECT title FROM todo_items WHERE status='backlog'").fetchall()]
cols = {row[1] for row in db1.execute("PRAGMA table_info(todo_items)")}
st = _safe_get("/todo")
db1.close()

check("A: import вернул успех (не flash ошибки)",
      "произошла ошибка" not in r.get_data(as_text=True).lower()
      and "Не удалось привести файл" not in r.get_data(as_text=True))
check("A: offline activation запросила restart", activate_status == 75,
      extra="status=%d" % activate_status)
check("A: АКТИВНАЯ БД содержит уникальный маркер сотрудника (импорт реально активирован)",
      MARK_EMP in post_names, extra="names=%s" % post_names)
check("A: АКТИВНАЯ БД содержит уникальный маркер задачи todo", MARK_TODO in todo_titles,
      extra="todos=%s" % todo_titles)
check("A: /todo после активации работает (200)", st == 200, extra="status=%d" % st)
check("A: добавлена колонка tag", "tag" in cols)
check("A: добавлена колонка due_date", "due_date" in cols)

# =====================================================================
# A-err) Ошибочный импорт (битые FK, не чинятся миграцией) -> отвергнут,
#        исходная БД сохранена (маркеры прежнего импорта на месте)
# =====================================================================
bad_path = os.path.join(tmp, "upload_bad.db")
b = sqlite3.connect(bad_path)
b.executescript("""
CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE departments (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE employees (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  position_id INTEGER, department_id INTEGER, salary TEXT DEFAULT '', active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE kb_columns (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'kanban', locked INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0);
CREATE TABLE kb_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,
  column_id INTEGER REFERENCES kb_columns(id) ON DELETE SET NULL, title TEXT NOT NULL DEFAULT '',
  description TEXT DEFAULT '', start_date TEXT DEFAULT '', due_date TEXT DEFAULT '',
  archived_at TEXT DEFAULT '', deleted_at TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
CREATE TABLE todo_items (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'backlog', sort_order INTEGER NOT NULL DEFAULT 0,
  assigned_date TEXT DEFAULT '', done_date TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')));
""")
b.execute("INSERT INTO kb_columns (name) VALUES ('Реальная')")      # id=1
b.execute("INSERT INTO kb_tasks (column_id, title) VALUES (999, 'сирота')")  # битый FK
b.commit()
b.close()

with open(bad_path, "rb") as f:
    bdata = f.read()
rb = _wrap_rpost("/backup/import", data={"dbfile": (BytesIO(bdata), "bad_restore.db")},
                 content_type="multipart/form-data", follow_redirects=True)
rb_html = rb.get_data(as_text=True).lower()

db2 = _open(appmod.DB_PATH)
names2 = [r["name"] for r in db2.execute("SELECT name FROM employees").fetchall()]
titles2 = [r["title"] for r in db2.execute("SELECT title FROM kb_tasks").fetchall()]
db2.close()

check("A-err: ошибка импорта отражена (flash 'Не удалось привести файл...')",
      "не удалось привести файл" in rb_html, extra="status=%d" % rb.status_code)
check("A-err: исходная БД сохранена — маркер сотрудника прежнего импорта на месте",
      MARK_EMP in names2, extra="names=%s" % names2)
check("A-err: данные битого файла НЕ активированы (нет 'сирота')",
      "сирота" not in titles2, extra="titles=%s" % titles2)

# =====================================================================
# D) Ошибочный импорт из-за НЕСОВМЕСТИМОЙ СХЕМЫ (отсутствие обязательных
#    колонок, напр. employees.salary) -> отвергнут, исходная (маркерная) БД
#    сохранена, success-flash отсутствует
# =====================================================================
def _build_broken(drop_col=None):
    import uuid
    path = os.path.join(tmp, "upl_" + uuid.uuid4().hex + ".db")
    marker = "СломанныйСхемаМаркер"
    con = sqlite3.connect(path)
    con.executescript(appmod.SCHEMA)
    con.execute("INSERT INTO employees (name) VALUES (?)", (marker,))
    con.commit()
    if drop_col is not None:
        tbl, col = drop_col
        try:
            con.execute("ALTER TABLE %s DROP COLUMN %s" % (tbl, col))
            con.commit()
        except Exception:
            con.close()
            os.remove(path)
            return None
    con.close()
    return path, marker


def _try_import_bad(bad_path):
    with open(bad_path, "rb") as f:
        bd = f.read()
    rr = _wrap_rpost("/backup/import", data={"dbfile": (BytesIO(bd), "bad.db")},
                     content_type="multipart/form-data", follow_redirects=True)
    return rr.get_data(as_text=True).lower()


def _bad_import_ok(html, names, marker):
    return ("не удалось привести файл" in html
            and "база восстановлена" not in html
            and "УникальныйСотрудникИмпорт" in names
            and marker not in names)


# --- D1: employees без salary (P2.14 исходный пример аудита) ---
res = _build_broken(("employees", "salary"))
if res:
    path, m = res
    html = _try_import_bad(path)
    os.remove(path)
    dbx = _open(appmod.DB_PATH)
    names = [r["name"] for r in dbx.execute("SELECT name FROM employees").fetchall()]
    dbx.close()
    check("D-salary: нет обязательной колонки salary -> импорт отклонён",
          "не удалось привести файл" in html and "база восстановлена" not in html)
    check("D-salary: исходная маркерная БД сохранена, маркер сломанного файла не активирован",
          "УникальныйСотрудникИмпорт" in names and m not in names,
          extra="names=%s" % names)

# --- D2: параметризованные отсутствующие обязательные колонки таблиц ---
# (notes/hire_date НЕ берём: их достраивает сама миграция -> импорт легитимно успешен)
cases = [
    ("employees", "name"),
    ("year_records", "goals_employee"),
    ("todo_items", "title"),
    ("kb_tasks", "title"),
    ("meetings", "summary"),
]
ran = 0
for tblcol in cases:
    res = _build_broken(tblcol)
    if not res:
        continue
    ran += 1
    path, m = res
    html = _try_import_bad(path)
    os.remove(path)
    dbx = _open(appmod.DB_PATH)
    names = [r["name"] for r in dbx.execute("SELECT name FROM employees").fetchall()]
    dbx.close()
    check("D2: нет обязательной колонки %s.%s -> импорт отвергнут, БД сохранена" % tblcol,
          _bad_import_ok(html, names, m),
          extra="err=%s;no_success=%s;prev_kept=%s;no_marker=%s" % (
              "не удалось привести файл" in html,
              "база восстановлена" not in html,
              "УникальныйСотрудникИмпорт" in names,
              m not in names))
check("D2: выполнено достаточно параметризованных сценариев (SQLite дропает не всё)",
      ran >= 5, extra="ran=%d cases=%d" % (ran, len(cases)))

# =====================================================================
# E) Ошибочный импорт из-за НЕСОВМЕСТИМЫХ СТРУКТУРНЫХ КОНТРАКТОВ схемы
#    (settings без PRIMARY KEY, year_records без UNIQUE, отсутствие FK-определения)
#    -> отвергнут, исходная БД сохранена
# =====================================================================
def _build_generic(transform):
    import uuid
    path = os.path.join(tmp, "upl_" + uuid.uuid4().hex + ".db")
    marker = "СломанныйСхемаМаркер"
    con = sqlite3.connect(path)
    con.executescript(appmod.SCHEMA)
    con.execute("INSERT INTO employees (name) VALUES (?)", (marker,))
    con.commit()
    try:
        transform(con)
        con.commit()
    except Exception:
        con.close()
        os.remove(path)
        return None
    con.close()
    return path, marker


def _e_case(label, transform):
    res = _build_generic(transform)
    if not res:
        check("E: %s — сломанный файл не построен" % label, False)
        return
    path, m = res
    html = _try_import_bad(path)
    os.remove(path)
    dbx = _open(appmod.DB_PATH)
    names = [r["name"] for r in dbx.execute("SELECT name FROM employees").fetchall()]
    dbx.close()
    check("E: %s -> импорт отвергнут, БД сохранена" % label,
          _bad_import_ok(html, names, m),
          extra="err=%s;no_success=%s;prev_kept=%s;no_marker=%s" % (
              "не удалось привести файл" in html,
              "база восстановлена" not in html,
              "УникальныйСотрудникИмпорт" in names,
              m not in names))


# settings без PRIMARY KEY: key отсутствует как PK -> INSERT OR REPLACE/ON CONFLICT сломан
def _tr_no_pk(c):
    c.execute("DROP TABLE settings")
    c.execute("CREATE TABLE settings (key TEXT, value TEXT)")


_e_case("settings без PRIMARY KEY (key)", _tr_no_pk)


# year_records без UNIQUE(employee_id,year,semester): ON CONFLICT в апдейте записей ломается
def _tr_no_unique(c):
    c.execute("DROP TABLE year_records")
    c.execute("""CREATE TABLE year_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        year INTEGER NOT NULL, semester TEXT NOT NULL,
        goals_employee TEXT DEFAULT '', proposals_manager TEXT DEFAULT '',
        wishes_employee TEXT DEFAULT '', comments TEXT DEFAULT '',
        colleagues_feedback TEXT DEFAULT '', updated_at TEXT DEFAULT (datetime('now'))
    )""")


_e_case("year_records без UNIQUE(employee_id,year,semester)", _tr_no_unique)


# отсутствие FK-определения column_id у kb_tasks (данных битых нет, но контракта нет)
def _tr_no_fk(c):
    c.execute("DROP TABLE kb_tasks")
    c.execute("""CREATE TABLE kb_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        column_id INTEGER,
        title TEXT NOT NULL DEFAULT '', description TEXT DEFAULT '',
        start_date TEXT DEFAULT '', due_date TEXT DEFAULT '',
        archived_at TEXT DEFAULT '', deleted_at TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
    )""")


_e_case("kb_tasks без FK column_id -> kb_columns(id)", _tr_no_fk)


# runtime полагается на ON DELETE CASCADE при удалении сотрудника ->
# FK контракт должен совпадать, не только (from,parent,to)
def _tr_no_cascade(c):
    c.execute("DROP TABLE problems")
    c.execute("""CREATE TABLE problems (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE NO ACTION,
        text TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )""")


_e_case("problems без ON DELETE CASCADE (NO ACTION)", _tr_no_cascade)


# employees.id должен быть INTEGER PRIMARY KEY (rowid): TEXT PK не генерирует id,
# код использует lastrowid -> ломает создание сотрудника
def _tr_text_pk(c):
    c.execute("CREATE TABLE emp_tmp ("
              "id TEXT PRIMARY KEY, name TEXT NOT NULL,"
              "position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL,"
              "department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,"
              "salary TEXT DEFAULT '', hire_date TEXT DEFAULT '', notes TEXT DEFAULT '',"
              "active INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')))")
    c.execute("INSERT INTO emp_tmp (id,name,position_id,department_id,salary,"
              "hire_date,notes,active,created_at) "
              "SELECT id,name,position_id,department_id,salary,hire_date,notes,"
              "active,created_at FROM employees")
    c.execute("DROP TABLE employees")
    c.execute("ALTER TABLE emp_tmp RENAME TO employees")


_e_case("employees.id TEXT PRIMARY KEY (не генерирует id)", _tr_text_pk)


# BIGINT PRIMARY KEY тоже НЕ rowid-alias: declared type != INTEGER даже при
# INTEGER-аффинити (создаёт pk-index) -> id не генерируется
def _tr_bigint_pk(c):
    c.execute("CREATE TABLE emp_tmp ("
              "id BIGINT PRIMARY KEY, name TEXT NOT NULL,"
              "position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL,"
              "department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,"
              "salary TEXT DEFAULT '', hire_date TEXT DEFAULT '', notes TEXT DEFAULT '',"
              "active INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')))")
    c.execute("INSERT INTO emp_tmp (id,name,position_id,department_id,salary,"
              "hire_date,notes,active,created_at) "
              "SELECT id,name,position_id,department_id,salary,hire_date,notes,"
              "active,created_at FROM employees")
    c.execute("DROP TABLE employees")
    c.execute("ALTER TABLE emp_tmp RENAME TO employees")


_e_case("employees.id BIGINT PRIMARY KEY (не rowid-alias)", _tr_bigint_pk)


# INTEGER PRIMARY KEY DESC не alias rowid даже при точном типе INTEGER
# (SQLite создаёт отдельный pk-index) -> id не генерируется
def _tr_integer_desc_pk(c):
    c.execute("CREATE TABLE emp_tmp ("
              "id INTEGER PRIMARY KEY DESC, name TEXT NOT NULL,"
              "position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL,"
              "department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,"
              "salary TEXT DEFAULT '', hire_date TEXT DEFAULT '', notes TEXT DEFAULT '',"
              "active INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')))")
    c.execute("INSERT INTO emp_tmp (id,name,position_id,department_id,salary,"
              "hire_date,notes,active,created_at) "
              "SELECT id,name,position_id,department_id,salary,hire_date,notes,"
              "active,created_at FROM employees")
    c.execute("DROP TABLE employees")
    c.execute("ALTER TABLE emp_tmp RENAME TO employees")


_e_case("employees.id INTEGER PRIMARY KEY DESC (не rowid-alias)", _tr_integer_desc_pk)

# =====================================================================
# F) Позитивный round-trip: свежая БД по актуальному SCHEMA импортируется
#    успешно (эталон == кандидат), маркер активируется
# =====================================================================
RT_MARK = "RoundTripСвежийМаркер"
_rtp = os.path.join(tmp, "rt_roundtrip.db")
_rcon = sqlite3.connect(_rtp)
_rcon.executescript(appmod.SCHEMA)
_rcon.execute("INSERT INTO employees (name) VALUES (?)", (RT_MARK,))
_rcon.commit()
_rcon.close()
with open(_rtp, "rb") as f:
    _rt = f.read()
_rr = _wrap_rpost("/backup/import", data={"dbfile": (BytesIO(_rt), "roundtrip.db")},
                  content_type="multipart/form-data", follow_redirects=True)
_rth = _rr.get_data(as_text=True).lower()
_rt_status = _activate_pending_restore()
_dbx = _open(appmod.DB_PATH)
_rtnames = [r["name"] for r in _dbx.execute("SELECT name FROM employees").fetchall()]
_dbx.close()
os.remove(_rtp)
check("F: актуальная свежая БД импортируется успешно (success flash, нет error)",
      "не удалось привести файл" not in _rth and _rt_status == 75,
      extra="status=%d" % _rt_status)
check("F: маркер round-trip активирован (импорт реально заменил БД)",
      RT_MARK in _rtnames, extra="names=%s" % _rtnames)

# =====================================================================
# B) Миграция: бэклог (locked=1) + emi-квадранты, все задачи (архив/корзина)
#    переносятся; foreign_key_check пуст
# =====================================================================
tmp2 = tempfile.mkdtemp()
appmod.DATA_DIR = tmp2
appmod.DB_PATH = os.path.join(tmp2, "hr_notes.db")

m = sqlite3.connect(appmod.DB_PATH)
m.executescript("""
CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE departments (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE employees (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  position_id INTEGER, department_id INTEGER, salary TEXT DEFAULT '', active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE kb_columns (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'kanban', locked INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0);
CREATE TABLE kb_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,
  column_id INTEGER REFERENCES kb_columns(id) ON DELETE SET NULL, title TEXT NOT NULL DEFAULT '',
  description TEXT DEFAULT '', start_date TEXT DEFAULT '', due_date TEXT DEFAULT '',
  archived_at TEXT DEFAULT '', deleted_at TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
CREATE TABLE todo_items (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'backlog', sort_order INTEGER NOT NULL DEFAULT 0,
  assigned_date TEXT DEFAULT '', done_date TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')));
""")
m.execute("INSERT INTO kb_columns (name, kind, locked, sort_order) VALUES ('Основной', 'kanban', 0, 10)")
m.execute("INSERT INTO kb_columns (name, kind, locked, sort_order) VALUES ('📥 Бэклог', 'kanban', 1, 0)")
m.execute("INSERT INTO kb_columns (name, kind, locked, sort_order) VALUES ('q1 old', 'emi1', 0, 20)")
m.execute("INSERT INTO kb_columns (name, kind, locked, sort_order) VALUES ('q2 old', 'emi2', 0, 30)")
bk = m.execute("SELECT id FROM kb_columns WHERE name='📥 Бэклог'").fetchone()[0]
q1 = m.execute("SELECT id FROM kb_columns WHERE name='q1 old'").fetchone()[0]
q2 = m.execute("SELECT id FROM kb_columns WHERE name='q2 old'").fetchone()[0]
m.execute("INSERT INTO kb_tasks (column_id, title, archived_at, deleted_at) VALUES (?, 'backlog live', '', '')", (bk,))
m.execute("INSERT INTO kb_tasks (column_id, title, archived_at, deleted_at) VALUES (?, 'backlog archived', '2026-01-01', '')", (bk,))
m.execute("INSERT INTO kb_tasks (column_id, title, archived_at, deleted_at) VALUES (?, 'backlog deleted', '', '2026-01-02')", (bk,))
m.execute("INSERT INTO kb_tasks (column_id, title, archived_at, deleted_at) VALUES (?, 'emi live', '', '')", (q1,))
m.execute("INSERT INTO kb_tasks (column_id, title, archived_at, deleted_at) VALUES (?, 'emi deleted', '', '2026-01-03')", (q2,))
m.commit()
m.close()

appmod.init_db()
db = _open(appmod.DB_PATH)
kinds = [r["kind"] for r in db.execute("SELECT kind FROM kb_columns").fetchall()]
rows = {r["title"]: r["column_id"] for r in db.execute("SELECT title, column_id FROM kb_tasks").fetchall()}
kanban_cols = {r["id"] for r in db.execute("SELECT id FROM kb_columns WHERE kind='kanban'").fetchall()}
fkbad = fk_check(db)
db.close()

check("B: emi-столбцы удалены, остался обычный kanban",
      "emi1" not in kinds and "emi2" not in kinds and len([k for k in kinds if k == "kanban"]) >= 1)
check("B: перенесены ВСЕ 5 задач (акт/архив/корзина) в живой kanban-столбец",
      len(rows) == 5 and all(cid in kanban_cols for cid in rows.values()),
      extra="titles=%s cols=%s kanban=%s" % (sorted(rows), list(rows.values()), sorted(kanban_cols)))
check("B: foreign_key_check пуст после миграции", fkbad == [], extra=str(fkbad))

# =====================================================================
# C) emi-only база (нет ни одного обычного kanban-столбца) -> создаётся
#    приёмник заранее, задачи живы, foreign_key_check пуст
# =====================================================================
tmp3 = tempfile.mkdtemp()
appmod.DATA_DIR = tmp3
appmod.DB_PATH = os.path.join(tmp3, "hr_notes.db")

e = sqlite3.connect(appmod.DB_PATH)
e.executescript("""
CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE departments (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE employees (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  position_id INTEGER, department_id INTEGER, salary TEXT DEFAULT '', active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE kb_columns (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'kanban', locked INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0);
CREATE TABLE kb_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,
  column_id INTEGER REFERENCES kb_columns(id) ON DELETE SET NULL, title TEXT NOT NULL DEFAULT '',
  description TEXT DEFAULT '', start_date TEXT DEFAULT '', due_date TEXT DEFAULT '',
  archived_at TEXT DEFAULT '', deleted_at TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
CREATE TABLE todo_items (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'backlog', sort_order INTEGER NOT NULL DEFAULT 0,
  assigned_date TEXT DEFAULT '', done_date TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')));
""")
e.execute("INSERT INTO kb_columns (name, kind, locked, sort_order) VALUES ('q1 old', 'emi1', 0, 20)")
e.execute("INSERT INTO kb_columns (name, kind, locked, sort_order) VALUES ('q2 old', 'emi2', 0, 30)")
eq1 = e.execute("SELECT id FROM kb_columns WHERE kind='emi1'").fetchone()[0]
e.execute("INSERT INTO kb_tasks (column_id, title) VALUES (?, 'emi-only t1')", (eq1,))
e.execute("INSERT INTO kb_tasks (column_id, title) VALUES (?, 'emi-only archived')", (eq1,))
e.commit()
e.close()

appmod.init_db()
db3 = _open(appmod.DB_PATH)
kinds3 = [r["kind"] for r in db3.execute("SELECT kind FROM kb_columns").fetchall()]
kanban3 = [r["id"] for r in db3.execute("SELECT id FROM kb_columns WHERE kind='kanban'").fetchall()]
rows3 = {r["title"]: r["column_id"] for r in db3.execute("SELECT title, column_id FROM kb_tasks").fetchall()}
fkbad3 = fk_check(db3)
db3.close()

check("C: emi-only база стала иметь обычный kanban-столбец",
      len(kanban3) >= 1 and "emi1" not in kinds3, extra="kinds=%s" % kinds3)
check("C: обе emi-задачи перенесены в созданный приёмник",
      len(rows3) == 2 and all(cid in kanban3 for cid in rows3.values()),
      extra="rows=%s kanban=%s" % (rows3, kanban3))
check("C: foreign_key_check пуст (emi-приёмник без битых FK)", fkbad3 == [], extra=str(fkbad3))

print()
if failures:
    print(f"ИТОГ: {len(failures)} ПРОВАЛЕНО -> {failures}")
    sys.exit(1)
print("ИТОГ: RESTORE/MIGRATION OK")