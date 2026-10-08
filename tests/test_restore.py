import importlib
import json
import os
import re
import sqlite3
import sys
from io import BytesIO

import pytest


OLD_SCHEMA = """
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
"""

LEGACY_BOARD_SCHEMA = """
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
"""


def open_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def login_with_csrf(app_module):
    client = app_module.app.test_client()
    response = client.post("/login", data={"password": "test-password"})
    assert response.status_code == 302
    with client.session_transaction() as session:
        session["_csrf"] = "restore-csrf-token"
    return client


def upload(client, path, filename="restore.db"):
    data = PathBytes(path)
    return client.post(
        "/backup/import",
        data={"_csrf": "restore-csrf-token", "dbfile": (BytesIO(data), filename)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def PathBytes(path):
    with open(path, "rb") as stream:
        return stream.read()


def activate_pending(app_module, tmp_path, monkeypatch):
    # Offline activation happens after all web request connections are closed.
    with app_module.app.app_context():
        app_module.close_db(None)
    # SQLite on Windows may leave WAL sidecars locked in this in-process model;
    # remove them as PID1 would after every web process has exited.
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(app_module.DB_PATH + suffix)
        except FileNotFoundError:
            pass
    sys.modules.pop("restore_offline", None)
    restore = importlib.import_module("restore_offline")
    with open(app_module.RESTORE_REQUEST, encoding="utf-8") as stream:
        staged = json.load(stream)["staged"]
    os.chmod(staged, 0o600)
    monkeypatch.setattr(restore, "DATA", str(tmp_path))
    monkeypatch.setattr(restore, "REQ", app_module.RESTORE_REQUEST)
    monkeypatch.setattr(restore, "LOCK", str(tmp_path / ".maintenance.lock"))
    monkeypatch.setattr(restore, "FATAL", str(tmp_path / ".restore-fatal"))
    monkeypatch.setattr(restore, "CURRENT", app_module.DB_PATH)
    monkeypatch.setattr(restore, "NAME_RE", re.compile(r"^\.restore-staged-[0-9a-f]{16}\.db$"))
    return restore.activate()


def employee_names(path):
    with open_db(path) as db:
        return [row["name"] for row in db.execute("SELECT name FROM employees")]


def build_current_db(app_module, path, marker="CandidateMarker"):
    with sqlite3.connect(path) as db:
        db.executescript(app_module.SCHEMA)
        db.execute("INSERT INTO employees(name) VALUES(?)", (marker,))
    return marker


def assert_rejected_and_active_preserved(app_module, client, candidate, active_marker):
    response = upload(client, candidate)
    html = response.get_data(as_text=True).lower()
    assert "не удалось привести файл" in html
    assert "база восстановлена" not in html
    names = employee_names(app_module.DB_PATH)
    assert active_marker in names
    assert "CandidateMarker" not in names


def test_two_phase_legacy_restore_migrates_then_activates(app_env, tmp_path, monkeypatch):
    employee_marker = "ImportedEmployeeMarker"
    todo_marker = "ImportedTodoMarker"
    candidate = tmp_path / "old.db"
    with sqlite3.connect(candidate) as db:
        db.executescript(OLD_SCHEMA)
        db.execute("INSERT INTO employees(name) VALUES(?)", (employee_marker,))
        db.execute("INSERT INTO todo_items(title) VALUES(?)", (todo_marker,))

    client = login_with_csrf(app_env)
    assert employee_marker not in employee_names(app_env.DB_PATH)
    response = upload(client, candidate)

    assert response.status_code == 200
    assert employee_marker not in employee_names(app_env.DB_PATH)
    assert os.path.exists(app_env.RESTORE_REQUEST)
    # The real activator runs after Gunicorn exits. Dispose the test client so
    # Windows releases its request-scoped SQLite WAL handles first.
    del client
    import gc
    gc.collect()
    assert activate_pending(app_env, tmp_path, monkeypatch) == 75

    with open_db(app_env.DB_PATH) as db:
        assert employee_marker in [row[0] for row in db.execute("SELECT name FROM employees")]
        assert todo_marker in [row[0] for row in db.execute("SELECT title FROM todo_items")]
        columns = {row[1] for row in db.execute("PRAGMA table_info(todo_items)")}
        assert {"tag", "due_date"} <= columns
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert login_with_csrf(app_env).get("/todo").status_code == 200


def test_current_schema_round_trip_is_two_phase(app_env, tmp_path, monkeypatch):
    candidate = tmp_path / "roundtrip.db"
    marker = build_current_db(app_env, candidate, "RoundTripMarker")
    client = login_with_csrf(app_env)

    response = upload(client, candidate)
    assert response.status_code == 200
    assert marker not in employee_names(app_env.DB_PATH)
    # The real activator runs after Gunicorn exits. Dispose the test client so
    # Windows releases its request-scoped SQLite WAL handles first.
    del client
    import gc
    gc.collect()
    assert activate_pending(app_env, tmp_path, monkeypatch) == 75
    assert marker in employee_names(app_env.DB_PATH)


def test_broken_foreign_keys_are_rejected_without_replacing_active_db(app_env, tmp_path):
    active_marker = "ActiveMarker"
    with sqlite3.connect(app_env.DB_PATH) as db:
        db.execute("INSERT INTO employees(name) VALUES(?)", (active_marker,))
    candidate = tmp_path / "bad-fk.db"
    with sqlite3.connect(candidate) as db:
        db.executescript(LEGACY_BOARD_SCHEMA)
        db.execute("INSERT INTO kb_columns(name) VALUES('valid')")
        db.execute("INSERT INTO kb_tasks(column_id,title) VALUES(999,'orphan')")

    response = upload(login_with_csrf(app_env), candidate)
    assert "не удалось привести файл" in response.get_data(as_text=True).lower()
    with open_db(app_env.DB_PATH) as db:
        assert active_marker in [row[0] for row in db.execute("SELECT name FROM employees")]
        assert "orphan" not in [row[0] for row in db.execute("SELECT title FROM kb_tasks")]


@pytest.mark.parametrize(
    "table,column",
    [
        ("employees", "salary"),
        ("employees", "name"),
        ("year_records", "goals_employee"),
        ("todo_items", "title"),
        ("kb_tasks", "title"),
        ("meetings", "summary"),
    ],
)
def test_missing_required_columns_are_rejected(app_env, tmp_path, table, column):
    active_marker = "ActiveMarker"
    with sqlite3.connect(app_env.DB_PATH) as db:
        db.execute("INSERT INTO employees(name) VALUES(?)", (active_marker,))
    candidate = tmp_path / f"missing-{table}-{column}.db"
    build_current_db(app_env, candidate)
    with sqlite3.connect(candidate) as db:
        db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    assert_rejected_and_active_preserved(app_env, login_with_csrf(app_env), candidate, active_marker)


def no_settings_pk(db):
    db.execute("DROP TABLE settings")
    db.execute("CREATE TABLE settings(key TEXT, value TEXT)")


def no_year_record_unique(db):
    db.execute("DROP TABLE year_records")
    db.execute("""CREATE TABLE year_records (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
      year INTEGER NOT NULL, semester TEXT NOT NULL, goals_employee TEXT DEFAULT '',
      proposals_manager TEXT DEFAULT '', wishes_employee TEXT DEFAULT '', comments TEXT DEFAULT '',
      colleagues_feedback TEXT DEFAULT '', updated_at TEXT DEFAULT (datetime('now')))""")


def no_task_fk(db):
    db.execute("DROP TABLE kb_tasks")
    db.execute("""CREATE TABLE kb_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,
      column_id INTEGER, title TEXT NOT NULL DEFAULT '', description TEXT DEFAULT '',
      start_date TEXT DEFAULT '', due_date TEXT DEFAULT '', archived_at TEXT DEFAULT '',
      deleted_at TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')),
      updated_at TEXT DEFAULT (datetime('now')))""")


def no_problem_cascade(db):
    db.execute("DROP TABLE problems")
    db.execute("""CREATE TABLE problems (id INTEGER PRIMARY KEY AUTOINCREMENT,
      employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE NO ACTION,
      text TEXT NOT NULL DEFAULT '', created_at TEXT DEFAULT (datetime('now')))""")


def rebuild_employee_pk(db, declaration):
    db.execute(f"""CREATE TABLE emp_tmp (id {declaration}, name TEXT NOT NULL,
      position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL,
      department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
      salary TEXT DEFAULT '', hire_date TEXT DEFAULT '', notes TEXT DEFAULT '',
      active INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')))""")
    db.execute("""INSERT INTO emp_tmp(id,name,position_id,department_id,salary,hire_date,notes,active,created_at)
      SELECT id,name,position_id,department_id,salary,hire_date,notes,active,created_at FROM employees""")
    db.execute("DROP TABLE employees")
    db.execute("ALTER TABLE emp_tmp RENAME TO employees")


@pytest.mark.parametrize(
    "transform",
    [
        no_settings_pk,
        no_year_record_unique,
        no_task_fk,
        no_problem_cascade,
        lambda db: rebuild_employee_pk(db, "TEXT PRIMARY KEY"),
        lambda db: rebuild_employee_pk(db, "BIGINT PRIMARY KEY"),
        lambda db: rebuild_employee_pk(db, "INTEGER PRIMARY KEY DESC"),
    ],
    ids=["settings-pk", "year-record-unique", "task-fk", "problem-cascade", "text-pk", "bigint-pk", "descending-pk"],
)
def test_broken_structural_contracts_are_rejected(app_env, tmp_path, transform):
    active_marker = "ActiveMarker"
    with sqlite3.connect(app_env.DB_PATH) as db:
        db.execute("INSERT INTO employees(name) VALUES(?)", (active_marker,))
    candidate = tmp_path / "broken-contract.db"
    build_current_db(app_env, candidate)
    with sqlite3.connect(candidate) as db:
        transform(db)
    assert_rejected_and_active_preserved(app_env, login_with_csrf(app_env), candidate, active_marker)


def create_legacy_board(path, emi_only=False):
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_BOARD_SCHEMA)
        if not emi_only:
            db.execute("INSERT INTO kb_columns(name,kind,locked,sort_order) VALUES('Main','kanban',0,10)")
            db.execute("INSERT INTO kb_columns(name,kind,locked,sort_order) VALUES('Backlog','kanban',1,0)")
        db.execute("INSERT INTO kb_columns(name,kind,locked,sort_order) VALUES('Q1','emi1',0,20)")
        db.execute("INSERT INTO kb_columns(name,kind,locked,sort_order) VALUES('Q2','emi2',0,30)")
        ids = {row[0]: row[1] for row in db.execute("SELECT name,id FROM kb_columns")}
        if not emi_only:
            db.executemany(
                "INSERT INTO kb_tasks(column_id,title,archived_at,deleted_at) VALUES(?,?,?,?)",
                [
                    (ids["Backlog"], "backlog live", "", ""),
                    (ids["Backlog"], "backlog archived", "2026-01-01", ""),
                    (ids["Backlog"], "backlog deleted", "", "2026-01-02"),
                ],
            )
        db.executemany(
            "INSERT INTO kb_tasks(column_id,title,archived_at,deleted_at) VALUES(?,?,?,?)",
            [(ids["Q1"], "emi live", "", ""), (ids["Q2"], "emi deleted", "", "2026-01-03")],
        )


@pytest.mark.parametrize("emi_only,expected_count", [(False, 5), (True, 2)])
def test_legacy_columns_rebind_all_tasks_to_live_kanban(app_env, tmp_path, emi_only, expected_count):
    data_dir = tmp_path / ("emi-only" if emi_only else "mixed")
    data_dir.mkdir()
    database = data_dir / "hr_notes.db"
    create_legacy_board(database, emi_only=emi_only)
    app_env.DATA_DIR = str(data_dir)
    app_env.DB_PATH = str(database)

    app_env.init_db()

    with open_db(database) as db:
        kinds = [row[0] for row in db.execute("SELECT kind FROM kb_columns")]
        kanban_ids = {row[0] for row in db.execute("SELECT id FROM kb_columns WHERE kind='kanban'")}
        tasks = db.execute("SELECT title,column_id FROM kb_tasks").fetchall()
        assert "emi1" not in kinds and "emi2" not in kinds
        assert kanban_ids
        assert len(tasks) == expected_count
        assert all(row["column_id"] in kanban_ids for row in tasks)
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
