# -*- coding: utf-8 -*-
"""Тест починки уже сломанной FK-ссылки (прод-сценарий):
БД где meetings/year_records ссылаются на employees_old (из-за бага ALT TABLE
RENAME в прошлой версии миграции). init_db() должна это починить."""
import os
import sqlite3
import tempfile
import sys

tmp = tempfile.mkdtemp()
os.environ["HR_DATA_DIR"] = tmp
os.environ["HR_PASSWORD"] = "x"

# Создаём БД в СОСТОЯНИИ ПОСЛЕ БАГА: employees есть, а meetings/year_records
# ссылаются на employees_old (которого нет).
db = sqlite3.connect(os.path.join(tmp, "hr_notes.db"))
db.executescript("""
CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE departments (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE employees (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  position_id INTEGER, department_id INTEGER, salary TEXT DEFAULT '',
  notes TEXT DEFAULT '', active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE year_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES "employees_old"(id) ON DELETE CASCADE,
  year INTEGER NOT NULL, semester TEXT NOT NULL,
  goals_employee TEXT DEFAULT '', proposals_manager TEXT DEFAULT '',
  wishes_employee TEXT DEFAULT '', comments TEXT DEFAULT '',
  updated_at TEXT DEFAULT (datetime('now')),
  UNIQUE(employee_id, year, semester)
);
CREATE TABLE meetings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES "employees_old"(id) ON DELETE CASCADE,
  date TEXT NOT NULL, summary TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now'))
);
""")
db.execute("INSERT INTO employees (id, name) VALUES (1, 'Иванов')")
db.execute("INSERT INTO meetings (employee_id, date, summary) VALUES (1, '2026-01-01', 'старая встреча')")
db.execute("INSERT INTO year_records (employee_id, year, semester) VALUES (1, 2026, '1H')")
db.commit()
db.close()

import app as appmod
appmod.init_db()

db = sqlite3.connect(os.path.join(tmp, "hr_notes.db"))
db.row_factory = sqlite3.Row
fail = []

for t in ("meetings", "year_records"):
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0]
    if "employees_old" in sql:
        fail.append(f"{t}: FK всё ещё на employees_old после починки")
    else:
        print(f"  {t}: FK починен -> references employees")

# данные не потеряны
m = db.execute("SELECT COUNT(*) c FROM meetings").fetchone()["c"]
y = db.execute("SELECT COUNT(*) c FROM year_records").fetchone()["c"]
print(f"  данные сохранены: встреч={m}, записей={y}")
if m != 1 or y != 1:
    fail.append("данные потеряны при починке")

# вставка после починки
try:
    db.execute("INSERT INTO meetings (employee_id, date, summary) VALUES (1, '2026-02-01', 'новая')")
    db.commit()
    print("  вставка встречи после починки — OK")
except Exception as e:
    fail.append(f"вставка после починки не работает: {e}")

db.close()
print()
if fail:
    print("ИТОГ: ПРОВАЛЫ ->", fail)
    sys.exit(1)
print("ИТОГ: ПОЧИНКА СЛОМАННОЙ БД ОК")