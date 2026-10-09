import datetime
import os
import re
import sqlite3

import pytest


def fetch_all(app_env, sql, args=()):
    with sqlite3.connect(app_env.DB_PATH) as db:
        db.row_factory = sqlite3.Row
        return db.execute(sql, args).fetchall()


def execute(app_env, sql, args=()):
    with sqlite3.connect(app_env.DB_PATH) as db:
        db.execute(sql, args)
        db.commit()


def csrf_post(client, url, data=None, token="audit-csrf-token", **kwargs):
    with client.session_transaction() as session:
        session["_csrf"] = token
    payload = dict(data or {})
    payload.setdefault("_csrf", token)
    return client.post(url, data=payload, **kwargs)


@pytest.mark.parametrize(
    ("data", "headers"),
    [
        ({"title": "no token"}, {}),
        ({"title": "wrong", "_csrf": "wrong"}, {}),
        (
            {"title": "foreign", "_csrf": "audit-csrf-token"},
            {"Origin": "http://evil.example"},
        ),
        ({"title": "cyr", "_csrf": "кириллицатокен"}, {}),
        ({"title": "cyr-header"}, {"X-CSRF-Token": "кириллицатокен"}),
    ],
    ids=["missing", "wrong", "foreign-origin", "cyrillic-form", "cyrillic-header"],
)
def test_csrf_rejects_invalid_requests(client, data, headers):
    if data.get("_csrf") == "audit-csrf-token":
        with client.session_transaction() as session:
            session["_csrf"] = "audit-csrf-token"
    response = client.post("/todo/add", data=data, headers=headers)
    assert response.status_code == 400


def test_csrf_accepts_valid_token_and_writes(app_env, client):
    response = csrf_post(client, "/todo/add", {"title": "CSRF-ok"})
    rows = fetch_all(app_env, "SELECT 1 FROM todo_items WHERE title='CSRF-ok'")
    assert response.status_code in (200, 302)
    assert len(rows) == 1


def test_board_badge_excludes_overdue_todo(app_env, client):
    execute(
        app_env,
        "INSERT INTO todo_items(title, status, due_date) VALUES (?, ?, ?)",
        ("todo overdue", "backlog", "2000-01-01"),
    )
    html = client.get("/").get_data(as_text=True)
    assert re.search(r'Доска<span class="nav-badge">(\d+)</span>', html) is None


def test_board_card_displays_dates_as_day_month(app_env, client):
    execute(app_env, "INSERT INTO kb_columns(name) VALUES (?)", ("Dates",))
    column_id = fetch_all(
        app_env, "SELECT id FROM kb_columns WHERE name='Dates'"
    )[0]["id"]
    execute(
        app_env,
        """INSERT INTO kb_tasks(column_id, title, start_date, due_date)
           VALUES (?, ?, ?, ?)""",
        (column_id, "Date format card", "2030-05-07", "2030-11-23"),
    )

    html = client.get("/board").get_data(as_text=True)

    assert "Срок: 07-05–23-11" in html
    assert "Срок: 05-07–11-23" not in html


def test_archive_links_to_next_day(client):
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    html = client.get("/todo/archive?date=" + yesterday).get_data(as_text=True)
    assert "Следующий день" in html
    assert datetime.date.today().isoformat() in html


def test_todo_delegation_preserves_due_date(app_env, client):
    assert csrf_post(client, "/board/column/add", {"name": "Work"}).status_code == 302
    execute(
        app_env,
        "INSERT INTO todo_items(title, status, due_date) VALUES (?, ?, ?)",
        ("дел с дедлайном", "backlog", "2030-05-05"),
    )
    todo_id = fetch_all(
        app_env, "SELECT id FROM todo_items WHERE title='дел с дедлайном'"
    )[0]["id"]

    response = csrf_post(client, f"/todo/{todo_id}/delegate")

    rows = fetch_all(
        app_env, "SELECT due_date FROM kb_tasks WHERE title='дел с дедлайном'"
    )
    assert response.status_code == 302
    assert [row["due_date"] for row in rows] == ["2030-05-05"]


def test_trashed_task_restores_after_column_deletion(app_env, client):
    csrf_post(client, "/board/column/add", {"name": "Purge column"})
    column_id = fetch_all(
        app_env, "SELECT id FROM kb_columns WHERE name='Purge column'"
    )[0]["id"]
    csrf_post(
        client,
        "/board/task/add",
        {"title": "trashed card", "column_id": str(column_id)},
    )
    task_id = fetch_all(
        app_env, "SELECT id FROM kb_tasks WHERE title='trashed card'"
    )[0]["id"]
    csrf_post(client, f"/board/task/{task_id}/trash")
    csrf_post(client, f"/board/column/{column_id}/delete")

    response = csrf_post(client, f"/board/task/{task_id}/restore")

    restored = fetch_all(
        app_env,
        "SELECT c.kind FROM kb_tasks t JOIN kb_columns c ON c.id=t.column_id "
        "WHERE t.id=?",
        (task_id,),
    )[0]
    assert response.status_code == 302
    assert restored["kind"] == "kanban"


def test_board_get_does_not_purge_old_trash(app_env, client):
    csrf_post(client, "/board/column/add", {"name": "Auto purge"})
    column_id = fetch_all(
        app_env, "SELECT id FROM kb_columns WHERE name='Auto purge'"
    )[0]["id"]
    csrf_post(
        client,
        "/board/task/add",
        {"title": "old trash", "column_id": str(column_id)},
    )
    task_id = fetch_all(
        app_env, "SELECT id FROM kb_tasks WHERE title='old trash'"
    )[0]["id"]
    old_ts = (datetime.datetime.now() - datetime.timedelta(days=40)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    execute(
        app_env,
        "UPDATE kb_tasks SET deleted_at=? WHERE id=?",
        (old_ts, task_id),
    )

    assert client.get("/board").status_code == 200
    assert len(fetch_all(app_env, "SELECT 1 FROM kb_tasks WHERE id=?", (task_id,))) == 1


def test_maintenance_purges_old_trash(app_env, monkeypatch):
    # app.py lazily relies on the stdlib module at runtime; keep this
    # conversion scoped to tests rather than changing production code.
    monkeypatch.setattr(app_env, "shutil", __import__("shutil"), raising=False)
    execute(app_env, "INSERT INTO kb_columns(name) VALUES (?)", ("Auto purge",))
    column_id = fetch_all(
        app_env, "SELECT id FROM kb_columns WHERE name='Auto purge'"
    )[0]["id"]
    old_ts = (datetime.datetime.now() - datetime.timedelta(days=40)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    execute(
        app_env,
        "INSERT INTO kb_tasks(column_id, title, deleted_at) VALUES (?, ?, ?)",
        (column_id, "old trash", old_ts),
    )
    task_id = fetch_all(
        app_env, "SELECT id FROM kb_tasks WHERE title='old trash'"
    )[0]["id"]

    app_env.run_maintenance()

    assert fetch_all(app_env, "SELECT 1 FROM kb_tasks WHERE id=?", (task_id,)) == []


def test_backup_export_removes_temporary_file(app_env, client, monkeypatch, tmp_path):
    probe = tmp_path / "probe_backup.db"

    def fake_mkstemp(**kwargs):
        return os.open(probe, os.O_CREAT | os.O_RDWR), str(probe)

    monkeypatch.setattr(app_env.tempfile, "mkstemp", fake_mkstemp)
    response = client.get("/backup/export")
    response.close()

    assert response.status_code == 200
    assert not probe.exists()


@pytest.fixture
def report_employee(app_env, client):
    response = csrf_post(
        client,
        "/employee/new",
        {"name": "Иван Сп/ециалист", "salary": "1"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    employee_id = fetch_all(
        app_env, "SELECT id FROM employees WHERE name='Иван Сп/ециалист'"
    )[0]["id"]
    execute(
        app_env,
        "INSERT INTO year_records(employee_id, year, semester, comments, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (employee_id, 2026, "1H", "комментарий с <b> незакрытым"),
    )
    return employee_id


@pytest.mark.parametrize(
    ("format_name", "signature"), [("xlsx", b"PK"), ("pdf", b"%PDF")]
)
def test_reports_handle_unsafe_name_and_markup(
    client, report_employee, format_name, signature
):
    response = client.get(
        f"/employee/{report_employee}/report?year=2026&format={format_name}"
    )
    assert response.status_code == 200
    assert response.data.startswith(signature)


def test_todo_backlog_is_drop_target(client):
    html = client.get("/todo").get_data(as_text=True)
    assert "todoDrop(event, 'backlog')" in html
    assert "ondragover" in html
    assert 'ondrop="todoDrop(event, \'backlog\')"' in html


def test_board_drag_end_resets_task_and_column_ids(app_env, client):
    execute(app_env, "INSERT INTO kb_columns(name) VALUES (?)", ("Drag column",))
    column_id = fetch_all(
        app_env, "SELECT id FROM kb_columns WHERE name='Drag column'"
    )[0]["id"]
    execute(
        app_env,
        "INSERT INTO kb_tasks(column_id, title) VALUES (?, ?)",
        (column_id, "Drag card"),
    )
    html = client.get("/board").get_data(as_text=True)
    assert 'ondragend="dragEnd(event)"' in html
    assert 'ondragstart="dragCol(event)" ondragend="dragEnd(event)"' in html
    assert "function dragEnd" in html
    assert "draggedId = null" in html
    assert "draggedColId = null" in html


def test_empty_admin_password_fails_closed(app_env):
    app_env.ADMIN_PASSWORD = ""
    anonymous = app_env.app.test_client()
    anonymous.post("/login", data={"password": ""}, follow_redirects=True)
    html = anonymous.get("/", follow_redirects=True).get_data(as_text=True)
    assert "Вход" in html
    assert "stats-row" not in html


def test_cyrillic_admin_password_logs_in(app_env):
    app_env.ADMIN_PASSWORD = "пароль-123"
    anonymous = app_env.app.test_client()
    response = anonymous.post(
        "/login", data={"password": "пароль-123"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert "Сотрудники" in response.get_data(as_text=True)
