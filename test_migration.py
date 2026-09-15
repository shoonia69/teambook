# -*- coding: utf-8 -*-
"""Тест миграции: старая БД (employees с TEXT position/department) -> новая схема."""
import os
import sqlite3
import tempfile
import sys

tmp = tempfile.mkdtemp()
os.environ["HR_DATA_DIR"] = tmp
os.environ["HR_PASSWORD"] = "x"

# Создаём СТАРУЮ базу (схема v1) с данными
db = sqlite3.connect(os.path.join(tmp, "hr_notes.db"))
db.executescript("""
CREATE TABLE employees (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    position      TEXT DEFAULT '',
    department    TEXT DEFAULT '',
    salary        TEXT DEFAULT '',
    active        INTEGER DEFAULT 1,
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE TABLE year_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    year INTEGER NOT NULL, semester TEXT NOT NULL,
    goals_employee TEXT DEFAULT '', proposals_manager TEXT DEFAULT '',
    wishes_employee TEXT DEFAULT '', comments TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(employee_id, year, semester)
);
CREATE TABLE meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    date TEXT NOT NULL, summary TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
""")
db.executemany(
    "INSERT INTO employees (name, position, department, salary) VALUES (?,?,?,?)",
    [
        ("Иванов", "Инженер 1 кат.", "ТП Orion soft", "120000"),
        ("Петров", "Инженер 1 кат.", "ТП Сбер", "110000"),
        ("Сидоров", "Стажёр", "ТП Orion soft", "50000"),
    ],
)
db.commit()
db.close()

import app as appmod
appmod.init_db()

db = sqlite3.connect(os.path.join(tmp, "hr_notes.db"))
db.row_factory = sqlite3.Row
fail = []

# проверяем справочники созданы корректно
pos = {r["name"]: r["id"] for r in db.execute("SELECT * FROM positions")}
dep = {r["name"]: r["id"] for r in db.execute("SELECT * FROM departments")}
print("должности:", sorted(pos))
print("отделы:", sorted(dep))
if set(pos) != {"Инженер 1 кат.", "Стажёр"}:
    fail.append("positions набор неверный")
if set(dep) != {"ТП Orion soft", "ТП Сбер"}:
    fail.append("departments набор неверный")

# проверяем employees связаны с справочниками
emps = db.execute("SELECT * FROM employees").fetchall()
for e in emps:
    # должность/отдел должны ссылаться на правильные id
    want_pos = ("Инженер 1 кат." if e["name"] != "Сидоров" else "Стажёр")
    want_dep = ("ТП Orion soft" if e["name"] in ("Иванов", "Сидоров") else "ТП Сбер")
    if pos[want_pos] != e["position_id"]:
        fail.append(f"{e['name']}: position_id неверный")
    if dep[want_dep] != e["department_id"]:
        fail.append(f"{e['name']}: department_id неверный")
    print(f"  {e['name']} -> {want_pos} ({e['position_id']}), {want_dep} ({e['department_id']})")

# идемпотентность: повторный init не должен ломать
appmod.init_db()
cnt = db.execute("SELECT COUNT(*) c FROM employees").fetchone()["c"]
if cnt != 3:
    fail.append(f"повторный init не идемпотентен: employees={cnt}")

# старые TEXT-колонки должны исчезнуть
cols = {r[1] for r in db.execute("PRAGMA table_info(employees)")}
if "position" in cols or "department" in cols:
    fail.append("старые TEXT-колонки не удалены")

# КРИТИЧНО: после миграции FK-ссылки в meetings/year_records должны указывать
# на employees (не на employees_old), и вставка должна работать.
for t in ("meetings", "year_records"):
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                     (t,)).fetchone()[0]
    if "employees_old" in sql:
        fail.append(f"FK в {t} указывает на employees_old (битая ссылка)")

# вставка встречи/записи после миграции не должна падать
try:
    with appmod.app.app_context():
        adb = appmod.get_db()
        adb.execute("INSERT INTO meetings (employee_id, date, summary) VALUES (1, '2026-01-01', 'тест')")
        adb.execute("INSERT INTO year_records (employee_id, year, semester, goals_employee) "
                    "VALUES (1, 2026, '1H', 'цель')")
        adb.commit()
    print("  вставка встречи и годовой записи после миграции — OK")
except Exception as e:
    fail.append(f"вставка после миграции не работает: {e}")

db.close()
print()
if fail:
    print("ИТОГ: ПРОВАЛЫ ->", fail)
    sys.exit(1)
print("ИТОГ: МИГРАЦИЯ ОК")