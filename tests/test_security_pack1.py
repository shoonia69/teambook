from io import BytesIO
import os
import sqlite3

import pytest


def db_connect(app_env):
    db = sqlite3.connect(app_env.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def csrf_client(client, token="security-csrf"):
    with client.session_transaction() as session:
        session["_csrf"] = token

    def post(url, data=None, **kwargs):
        payload = dict(data or {})
        payload.setdefault("_csrf", token)
        return client.post(url, data=payload, **kwargs)

    return post


def add_column_and_task(app_env, client):
    post = csrf_client(client)
    assert post("/board/column/add", {"name": "Valid column"}).status_code == 302
    with db_connect(app_env) as db:
        column_id = db.execute(
            "SELECT id FROM kb_columns WHERE name='Valid column'"
        ).fetchone()["id"]
    assert post(
        "/board/task/add", {"title": "valid task", "column_id": str(column_id)}
    ).status_code == 302
    with db_connect(app_env) as db:
        task_id = db.execute(
            "SELECT id FROM kb_tasks WHERE title='valid task'"
        ).fetchone()["id"]
    return post, column_id, task_id


def test_login_limiter_blocks_threshold_and_valid_password(app_env):
    attacker = app_env.app.test_client()
    statuses = [
        attacker.post(
            "/login",
            data={"password": "wrong"},
            environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
        ).status_code
        for _ in range(5)
    ]
    blocked_valid = attacker.post(
        "/login",
        data={"password": "test-password"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
    )

    assert statuses[-1] == 429
    assert blocked_valid.status_code == 429


def test_login_limiter_is_ip_isolated_and_sqlite_backed(app_env):
    attacker = app_env.app.test_client()
    for _ in range(5):
        attacker.post(
            "/login",
            data={"password": "wrong"},
            environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
        )
    other = app_env.app.test_client()
    response = other.post(
        "/login",
        data={"password": "test-password"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.11"},
    )
    with db_connect(app_env) as db:
        stored = db.execute(
            "SELECT COUNT(*) FROM login_failures WHERE ip='203.0.113.10'"
        ).fetchone()[0]

    assert response.status_code == 302
    assert stored == 5


def test_session_cookie_hardening_is_configured(app_env):
    config = app_env.app.config
    assert config["SESSION_COOKIE_HTTPONLY"] is True
    assert config["SESSION_COOKIE_SAMESITE"] in ("Lax", "Strict")
    assert config["PERMANENT_SESSION_LIFETIME"]


def test_logout_is_post_only_and_clears_entire_session(client):
    post = csrf_client(client)

    assert client.get("/logout").status_code in (404, 405)
    response = post("/logout")

    assert response.status_code == 302
    assert "/login" in response.location
    with client.session_transaction() as session:
        assert "authed" not in session
        assert "_csrf" not in session


def test_board_add_rejects_unknown_column_without_write(app_env, client):
    post = csrf_client(client)
    with db_connect(app_env) as db:
        before = db.execute("SELECT COUNT(*) FROM kb_tasks").fetchone()[0]

    response = post(
        "/board/task/add", {"title": "bad add", "column_id": "999999"}
    )

    with db_connect(app_env) as db:
        after = db.execute("SELECT COUNT(*) FROM kb_tasks").fetchone()[0]
    assert response.status_code == 400
    assert after == before


@pytest.mark.parametrize("action", ["move", "edit"])
def test_board_mutation_rejects_unknown_column_without_change(
    app_env, client, action
):
    post, column_id, task_id = add_column_and_task(app_env, client)
    with db_connect(app_env) as db:
        before = db.execute(
            "SELECT * FROM kb_tasks WHERE id=?", (task_id,)
        ).fetchone()
    payload = {"column_id": "999999"}
    if action == "edit":
        payload.update(
            title="valid task", description="", start_date="", due_date=""
        )

    response = post(f"/board/task/{task_id}/{action}", payload)

    with db_connect(app_env) as db:
        after = db.execute(
            "SELECT * FROM kb_tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert response.status_code == 400
    assert after == before
    assert after["column_id"] == column_id


def test_board_restore_replaces_non_kanban_column(app_env, client):
    post, _, task_id = add_column_and_task(app_env, client)
    with db_connect(app_env) as db:
        legacy_id = db.execute(
            "INSERT INTO kb_columns(name, kind, sort_order) VALUES (?, ?, ?)",
            ("legacy", "legacy", 999),
        ).lastrowid
        db.execute(
            "UPDATE kb_tasks SET column_id=?, archived_at=datetime('now') WHERE id=?",
            (legacy_id, task_id),
        )
        db.commit()

    response = post(f"/board/task/{task_id}/restore")

    with db_connect(app_env) as db:
        restored = db.execute(
            "SELECT c.kind FROM kb_tasks t JOIN kb_columns c ON c.id=t.column_id "
            "WHERE t.id=?",
            (task_id,),
        ).fetchone()
    assert response.status_code == 302
    assert restored["kind"] == "kanban"


def test_backup_upload_limit_is_configured(app_env):
    limit = app_env.app.config["MAX_CONTENT_LENGTH"]
    assert isinstance(limit, int)
    assert limit > 0


def test_oversized_backup_upload_returns_413(app_env, client):
    post = csrf_client(client)
    old_limit = app_env.app.config["MAX_CONTENT_LENGTH"]
    app_env.app.config["MAX_CONTENT_LENGTH"] = 1024
    try:
        response = post(
            "/backup/import",
            {"dbfile": (BytesIO(b"x" * 4096), "huge.db")},
            content_type="multipart/form-data",
        )
    finally:
        app_env.app.config["MAX_CONTENT_LENGTH"] = old_limit
    assert response.status_code == 413


@pytest.mark.parametrize(
    "value", ["=1+1", "+SUM(A1:A2)", "-1+2", "@cmd", "  =HYPERLINK('x')"]
)
def test_xlsx_sanitizer_neutralizes_formula_values(app_env, value):
    safe = app_env._xlsx_safe(value)
    assert isinstance(safe, str)
    assert safe.lstrip().startswith("'")


def test_xlsx_sanitizer_preserves_ordinary_text(app_env):
    assert app_env._xlsx_safe("Иванов") == "Иванов"


@pytest.mark.parametrize("object_type", ["table", "trigger", "view"])
def test_restore_rejects_unknown_schema_objects(
    app_env, client, tmp_path, object_type
):
    post = csrf_client(client)
    upload = tmp_path / f"restore_{object_type}.db"
    with db_connect(app_env) as source, sqlite3.connect(upload) as destination:
        source.backup(destination)
        if object_type == "table":
            destination.execute("CREATE TABLE audit_extra(id INTEGER)")
        elif object_type == "trigger":
            destination.execute(
                "CREATE TRIGGER evil_trigger AFTER INSERT ON employees "
                "BEGIN DELETE FROM employees; END"
            )
        else:
            destination.execute("CREATE VIEW evil_view AS SELECT * FROM employees")
        destination.commit()

    response = post(
        "/backup/import",
        {"dbfile": (BytesIO(upload.read_bytes()), upload.name)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    html = response.get_data(as_text=True).lower()
    assert any(marker in html for marker in ("не удалось", "невалид", "не похож"))
