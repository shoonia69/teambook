import sqlite3

import pytest


@pytest.fixture
def validation_records(app_env):
    db = sqlite3.connect(app_env.DB_PATH)
    db.execute("INSERT INTO employees(name) VALUES(?)", ("Employee",))
    employee_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO kb_columns(name) VALUES(?)", ("Work",))
    column_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO todo_items(title) VALUES(?)", ("Todo",))
    todo_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO kb_tasks(column_id, title, start_date, due_date) "
        "VALUES(?, ?, ?, ?)",
        (column_id, "Existing", "2026-01-01", "2026-01-02"),
    )
    task_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    db.close()
    return {
        "employee_id": employee_id,
        "column_id": column_id,
        "todo_id": todo_id,
        "task_id": task_id,
    }


@pytest.mark.parametrize(
    ("label", "route", "payload", "table", "row_id_name", "is_create"),
    [
        (
            "invalid semester",
            "/employee/{employee_id}/record",
            {"year": "2026", "semester": "3H"},
            "year_records",
            None,
            True,
        ),
        (
            "invalid board date",
            "/board/task/add",
            {
                "title": "Invalid date",
                "column_id": "{column_id}",
                "start_date": "2026-02-30",
                "due_date": "",
            },
            "kb_tasks",
            None,
            True,
        ),
        (
            "reversed board range",
            "/board/task/add",
            {
                "title": "Reversed range",
                "column_id": "{column_id}",
                "start_date": "2026-03-02",
                "due_date": "2026-03-01",
            },
            "kb_tasks",
            None,
            True,
        ),
        (
            "invalid board edit",
            "/board/task/{task_id}/edit",
            {
                "title": "Changed",
                "column_id": "{column_id}",
                "start_date": "2026-03-02",
                "due_date": "2026-03-01",
            },
            "kb_tasks",
            "task_id",
            False,
        ),
        (
            "invalid todo date",
            "/todo/{todo_id}/edit",
            {"title": "Todo", "due_date": "2026-02-30", "tag": ""},
            "todo_items",
            "todo_id",
            False,
        ),
    ],
    ids=lambda case: case if isinstance(case, str) else None,
)
def test_http_validation_rejects_without_partial_writes(
    app_env,
    client,
    validation_records,
    label,
    route,
    payload,
    table,
    row_id_name,
    is_create,
):
    db = sqlite3.connect(app_env.DB_PATH)
    before_count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    before_row = None
    if row_id_name:
        before_row = db.execute(
            f"SELECT * FROM {table} WHERE id=?",
            (validation_records[row_id_name],),
        ).fetchone()
    db.close()

    formatted_route = route.format(**validation_records)
    formatted_payload = {
        key: value.format(**validation_records)
        for key, value in payload.items()
    }
    response = client.post(formatted_route, data=formatted_payload)

    assert response.status_code == 400, label

    db = sqlite3.connect(app_env.DB_PATH)
    after_count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if is_create:
        assert after_count == before_count, label
    else:
        after_row = db.execute(
            f"SELECT * FROM {table} WHERE id=?",
            (validation_records[row_id_name],),
        ).fetchone()
        assert after_row == before_row, label
    db.close()
