import sqlite3

import pytest


BROKEN_SCHEMA = """
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
"""


@pytest.fixture
def repaired_db(app_env, tmp_path):
    path = tmp_path / "broken-fk.db"
    db = sqlite3.connect(path)
    db.executescript(BROKEN_SCHEMA)
    db.execute("INSERT INTO employees(id,name) VALUES (?,?)", (1, "Иванов"))
    db.execute(
        "INSERT INTO meetings(employee_id,date,summary) VALUES (?,?,?)",
        (1, "2026-01-01", "старая встреча"),
    )
    db.execute(
        "INSERT INTO year_records(employee_id,year,semester) VALUES (?,?,?)",
        (1, 2026, "1H"),
    )
    db.commit()
    db.close()

    app_env.DATA_DIR = str(tmp_path)
    app_env.DB_PATH = str(path)
    app_env.init_db()
    return path


@pytest.mark.parametrize("table", ["meetings", "year_records"])
def test_dangling_foreign_key_is_repaired(repaired_db, table):
    db = sqlite3.connect(repaired_db)
    try:
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        targets = {row[2] for row in db.execute(f"PRAGMA foreign_key_list({table})")}
    finally:
        db.close()
    assert "employees_old" not in ddl
    assert "employees" in targets


def test_repair_preserves_existing_child_rows(repaired_db):
    db = sqlite3.connect(repaired_db)
    try:
        meeting = db.execute(
            "SELECT employee_id,date,summary FROM meetings"
        ).fetchone()
        year_record = db.execute(
            "SELECT employee_id,year,semester FROM year_records"
        ).fetchone()
    finally:
        db.close()
    assert meeting == (1, "2026-01-01", "старая встреча")
    assert year_record == (1, 2026, "1H")


def test_child_insert_succeeds_after_repair(repaired_db):
    db = sqlite3.connect(repaired_db)
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute(
            "INSERT INTO meetings(employee_id,date,summary) VALUES (?,?,?)",
            (1, "2026-02-01", "новая"),
        )
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()


def test_repair_is_idempotent(app_env, repaired_db):
    app_env.init_db()
    db = sqlite3.connect(repaired_db)
    try:
        assert db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM year_records").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()
