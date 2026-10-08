import sqlite3

import pytest


OLD_SCHEMA = """
CREATE TABLE employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    position TEXT DEFAULT '',
    department TEXT DEFAULT '',
    salary TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
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
"""

EMPLOYEES = [
    ("Иванов", "Инженер 1 кат.", "ТП Orion soft", "120000"),
    ("Петров", "Инженер 1 кат.", "ТП Сбер", "110000"),
    ("Сидоров", "Стажёр", "ТП Orion soft", "50000"),
]


@pytest.fixture
def migrated_legacy_db(app_env, tmp_path):
    path = tmp_path / "legacy-v1.db"
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)
    db.executemany(
        "INSERT INTO employees(name,position,department,salary) VALUES (?,?,?,?)",
        EMPLOYEES,
    )
    db.commit()
    db.close()
    app_env.DATA_DIR = str(tmp_path)
    app_env.DB_PATH = str(path)
    app_env.init_db()
    return path


def open_rows(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def test_migration_creates_deduplicated_reference_tables(migrated_legacy_db):
    db = open_rows(migrated_legacy_db)
    try:
        positions = {row["name"] for row in db.execute("SELECT name FROM positions")}
        departments = {row["name"] for row in db.execute("SELECT name FROM departments")}
    finally:
        db.close()
    assert positions == {"Инженер 1 кат.", "Стажёр"}
    assert departments == {"ТП Orion soft", "ТП Сбер"}


def test_migration_preserves_employee_reference_mapping(migrated_legacy_db):
    db = open_rows(migrated_legacy_db)
    try:
        positions = {row["name"]: row["id"] for row in db.execute("SELECT * FROM positions")}
        departments = {row["name"]: row["id"] for row in db.execute("SELECT * FROM departments")}
        employees = db.execute("SELECT * FROM employees ORDER BY id").fetchall()
    finally:
        db.close()
    expected = {
        "Иванов": ("Инженер 1 кат.", "ТП Orion soft"),
        "Петров": ("Инженер 1 кат.", "ТП Сбер"),
        "Сидоров": ("Стажёр", "ТП Orion soft"),
    }
    assert len(employees) == 3
    for employee in employees:
        position, department = expected[employee["name"]]
        assert employee["position_id"] == positions[position]
        assert employee["department_id"] == departments[department]


def test_migration_removes_legacy_text_columns(migrated_legacy_db):
    db = sqlite3.connect(migrated_legacy_db)
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(employees)")}
    finally:
        db.close()
    assert "position" not in columns
    assert "department" not in columns
    assert {"position_id", "department_id"} <= columns


def test_migration_is_idempotent_and_preserves_rows(app_env, migrated_legacy_db):
    app_env.init_db()
    db = sqlite3.connect(migrated_legacy_db)
    try:
        assert db.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 3
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


@pytest.mark.parametrize("table", ["meetings", "year_records"])
def test_child_foreign_keys_still_reference_employees(migrated_legacy_db, table):
    db = sqlite3.connect(migrated_legacy_db)
    try:
        targets = {row[2] for row in db.execute(f"PRAGMA foreign_key_list({table})")}
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
    finally:
        db.close()
    assert "employees" in targets
    assert "employees_old" not in ddl


def test_child_inserts_work_after_employee_migration(app_env, migrated_legacy_db):
    with app_env.app.app_context():
        db = app_env.get_db()
        db.execute(
            "INSERT INTO meetings(employee_id,date,summary) VALUES (?,?,?)",
            (1, "2026-01-01", "тест"),
        )
        db.execute(
            "INSERT INTO year_records(employee_id,year,semester,goals_employee) VALUES (?,?,?,?)",
            (1, 2026, "1H", "цель"),
        )
        db.commit()

    db = sqlite3.connect(migrated_legacy_db)
    try:
        assert db.execute("SELECT summary FROM meetings").fetchone()[0] == "тест"
        assert db.execute("SELECT goals_employee FROM year_records").fetchone()[0] == "цель"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()
