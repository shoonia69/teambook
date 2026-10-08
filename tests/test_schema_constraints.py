import re
import sqlite3

import pytest


@pytest.fixture
def seeded_db(app_env):
    db = sqlite3.connect(app_env.DB_PATH)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("INSERT INTO employees(name) VALUES (?)", ("Тест",))
    employee_id = db.execute("SELECT id FROM employees").fetchone()[0]
    db.execute("INSERT INTO kb_columns(name) VALUES (?)", ("Работа",))
    column_id = db.execute("SELECT id FROM kb_columns").fetchone()[0]
    db.commit()
    try:
        yield db, employee_id, column_id
    finally:
        db.close()


@pytest.mark.parametrize(
    "sql,params",
    [
        ("INSERT INTO todo_items(title,status) VALUES (?,?)", ("x", "bad")),
        (
            "INSERT INTO todo_items(title,status,done_date) VALUES (?,?,?)",
            ("x", "done", None),
        ),
        ("INSERT INTO todo_items(title,due_date) VALUES (?,?)", ("x", "2026-02-30")),
        (
            "INSERT INTO todo_items(title,status,done_date) VALUES (?,?,?)",
            ("x", "backlog", "2026-01-01"),
        ),
    ],
    ids=["todo-status", "todo-null-done-date", "todo-invalid-date", "todo-done-state"],
)
def test_todo_constraints_reject_invalid_rows(seeded_db, sql, params):
    db, _, _ = seeded_db
    before = db.execute("SELECT COUNT(*) FROM todo_items").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, params)
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM todo_items").fetchone()[0] == before


def test_year_record_semester_constraint_rejects_invalid_value(seeded_db):
    db, employee_id, _ = seeded_db
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO year_records(employee_id,year,semester) VALUES (?,?,?)",
            (employee_id, 2026, "3H"),
        )
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM year_records").fetchone()[0] == 0


@pytest.mark.parametrize(
    "start_date,due_date",
    [
        ("2026-02-30", "2026-03-01"),
        (None, ""),
        ("2026-03-02", "2026-03-01"),
    ],
    ids=["invalid-date", "null-date", "reversed-range"],
)
def test_kanban_date_constraints_reject_invalid_rows(seeded_db, start_date, due_date):
    db, _, column_id = seeded_db
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO kb_tasks(column_id,title,start_date,due_date) VALUES (?,?,?,?)",
            (column_id, "x", start_date, due_date),
        )
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM kb_tasks").fetchone()[0] == 0


def test_kanban_foreign_key_rejects_missing_column(seeded_db):
    db, _, _ = seeded_db
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO kb_tasks(column_id,title) VALUES (?,?)", (999999, "x"))
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM kb_tasks").fetchone()[0] == 0


def unconstrained_schema(app_env):
    schema = app_env.SCHEMA.replace(" CHECK (semester IN ('1H', '2H'))", "")
    for marker in (
        " CONSTRAINT ck_year_records_semester",
        " CONSTRAINT ck_kb_tasks_start_date",
        " CONSTRAINT ck_kb_tasks_due_date",
        " CONSTRAINT ck_todo_status",
        " CONSTRAINT ck_todo_done_date",
        " CONSTRAINT ck_todo_due_date",
        "    CONSTRAINT ck_kb_tasks_date_order\n",
        "    CONSTRAINT ck_todo_done_state\n",
    ):
        schema = schema.replace(marker, "")
    return (
        schema.replace(
            "\n                  CHECK (status IN ('backlog','q_iu','q_in','q_nu','q_nn','done'))",
            "",
        )
        .replace(" CHECK (done_date = '' OR date(done_date) = done_date)", "")
        .replace(" CHECK (due_date = '' OR date(due_date) = due_date)", "")
        .replace(" CHECK (start_date = '' OR date(start_date) = start_date)", "")
        .replace(
            ",\n    CHECK (start_date = '' OR due_date = '' OR start_date <= due_date)",
            "",
        )
        .replace(
            ",\n    CHECK ((status = 'done' AND done_date != '') OR (status != 'done' AND done_date = ''))",
            "",
        )
    )


def test_legacy_invalid_values_are_normalized_and_extra_columns_removed(app_env, tmp_path):
    legacy_path = tmp_path / "legacy.db"
    db = sqlite3.connect(legacy_path)
    db.executescript(unconstrained_schema(app_env))
    db.execute("INSERT INTO employees(name) VALUES ('Legacy')")
    employee_id = db.execute("SELECT id FROM employees").fetchone()[0]
    db.execute(
        "INSERT INTO year_records(employee_id,year,semester) VALUES (?,?,?)",
        (employee_id, 2026, "3H"),
    )
    db.execute("ALTER TABLE year_records ADD COLUMN space_owner INTEGER")
    db.execute("UPDATE year_records SET space_owner=42")
    db.execute(
        "INSERT INTO todo_items(title,status,done_date,due_date) VALUES (?,?,?,?)",
        ("legacy", "today", "2026-01-01", "broken"),
    )
    db.execute("INSERT INTO kb_columns(name) VALUES ('Legacy')")
    column_id = db.execute("SELECT id FROM kb_columns").fetchone()[0]
    db.execute(
        "INSERT INTO kb_tasks(column_id,title,start_date,due_date) VALUES (?,?,?,?)",
        (column_id, "legacy", "broken", "2026-01-01"),
    )
    db.commit()
    db.close()

    app_env.DATA_DIR = str(tmp_path)
    app_env.DB_PATH = str(legacy_path)
    app_env.init_db()
    app_env.init_db()

    db = sqlite3.connect(legacy_path)
    try:
        assert db.execute(
            "SELECT status,done_date,due_date FROM todo_items"
        ).fetchone() == ("q_iu", "", "")
        assert db.execute("SELECT semester FROM year_records").fetchone()[0] == "1H"
        assert db.execute("SELECT start_date,due_date FROM kb_tasks").fetchone() == (
            "",
            "2026-01-01",
        )
        assert "space_owner" not in {
            row[1] for row in db.execute("PRAGMA table_info(year_records)")
        }
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def test_restore_validator_rejects_decoy_checks(app_env):
    db = sqlite3.connect(":memory:")
    try:
        schema = unconstrained_schema(app_env).replace(
            "year INTEGER NOT NULL,", "year INTEGER NOT NULL CHECK (year > 0),"
        ).replace(
            "sort_order    INTEGER NOT NULL DEFAULT 0,",
            "sort_order    INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),",
        )
        db.executescript(schema)
        errors = app_env._validate_db_schema(db)
    finally:
        db.close()
    assert any("CHECK-контракт" in error for error in errors)


def test_restore_validator_rejects_forged_named_checks(app_env):
    schema = re.sub(
        r"(CONSTRAINT ck_todo_[a-z_]+\s+CHECK\s*)\((?:[^()]|\([^()]*\))*\)",
        r"\1(1)",
        app_env.SCHEMA,
    )
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(schema)
        errors = app_env._validate_db_schema(db)
    finally:
        db.close()
    assert any("неверные CHECK-контракты" in error for error in errors)


def test_restore_validator_rejects_duplicate_named_checks(app_env):
    schema = app_env.SCHEMA.replace(
        "status        TEXT NOT NULL DEFAULT 'backlog' CONSTRAINT ck_todo_status",
        "status        TEXT NOT NULL DEFAULT 'backlog' "
        "CONSTRAINT ck_todo_status CHECK (1) CONSTRAINT ck_todo_status",
        1,
    )
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(schema)
        errors = app_env._validate_db_schema(db)
    finally:
        db.close()
    assert any("неверные CHECK-контракты" in error for error in errors)
