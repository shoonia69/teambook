import sqlite3

import pytest


@pytest.fixture
def employee_records(app_env):
    db = sqlite3.connect(app_env.DB_PATH)
    db.execute(
        "INSERT INTO employees(name, hire_date) VALUES(?, ?)",
        ("Employee", "2020-01-01"),
    )
    employee_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO employee_history(employee_id, change_date) VALUES(?, ?)",
        (employee_id, "2020-01-01"),
    )
    history_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO meetings(employee_id, date) VALUES(?, ?)",
        (employee_id, "2020-01-01"),
    )
    meeting_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    db.close()
    return employee_id, history_id, meeting_id


@pytest.mark.parametrize(
    ("label", "route", "payload", "table", "row_id_name", "is_create"),
    [
        (
            "employee create",
            "/employee/new",
            {"name": "Bad", "hire_date": "2026-02-30"},
            "employees",
            None,
            True,
        ),
        (
            "employee edit",
            "/employee/{employee_id}/edit",
            {"name": "Employee", "hire_date": "2026-02-30"},
            "employees",
            "employee_id",
            False,
        ),
        (
            "history create",
            "/employee/{employee_id}/history/add",
            {"change_date": "2026-02-30"},
            "employee_history",
            None,
            True,
        ),
        (
            "history edit",
            "/history/{history_id}/edit",
            {"change_date": "2026-02-30"},
            "employee_history",
            "history_id",
            False,
        ),
        (
            "meeting create",
            "/employee/{employee_id}/meeting/new",
            {"date": "2026-02-30"},
            "meetings",
            None,
            True,
        ),
        (
            "meeting edit",
            "/meeting/{meeting_id}/edit",
            {"date": "2026-02-30"},
            "meetings",
            "meeting_id",
            False,
        ),
    ],
    ids=lambda case: case if isinstance(case, str) else None,
)
def test_invalid_domain_dates_return_400(
    client,
    app_env,
    employee_records,
    label,
    route,
    payload,
    table,
    row_id_name,
    is_create,
):
    employee_id, history_id, meeting_id = employee_records
    ids = {
        "employee_id": employee_id,
        "history_id": history_id,
        "meeting_id": meeting_id,
    }
    db = sqlite3.connect(app_env.DB_PATH)
    before_count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    before_row = None
    if row_id_name:
        before_row = db.execute(
            f"SELECT * FROM {table} WHERE id=?",
            (ids[row_id_name],),
        ).fetchone()
    db.close()

    response = client.post(
        route.format(
            employee_id=employee_id,
            history_id=history_id,
            meeting_id=meeting_id,
        ),
        data=payload,
    )

    assert response.status_code == 400, label

    db = sqlite3.connect(app_env.DB_PATH)
    after_count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if is_create:
        assert after_count == before_count, label
    else:
        after_row = db.execute(
            f"SELECT * FROM {table} WHERE id=?",
            (ids[row_id_name],),
        ).fetchone()
        assert after_row == before_row, label
    db.close()
