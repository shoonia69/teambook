import sqlite3
from datetime import datetime, timedelta

import pytest


REFERENCE_TIME = datetime(2026, 10, 8, 12, 0, 0)


@pytest.fixture
def maintenance_tasks(app_env):
    old = (REFERENCE_TIME - timedelta(days=30, seconds=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    edge = (REFERENCE_TIME - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = (REFERENCE_TIME - timedelta(days=29)).strftime("%Y-%m-%d %H:%M:%S")

    db = sqlite3.connect(app_env.DB_PATH)
    try:
        db.execute(
            "INSERT INTO kb_columns(name, kind, locked, sort_order) "
            "VALUES(?, 'kanban', 0, 0)",
            ("Основная",),
        )
        column_id = db.execute(
            "SELECT id FROM kb_columns WHERE name=?",
            ("Основная",),
        ).fetchone()[0]
        rows = [
            (column_id, "OLD_TRASH", "", old),
            (column_id, "EDGE_TRASH", "", edge),
            (column_id, "NEW_TRASH", "", fresh),
            (column_id, "ARCHIVED", old, ""),
            (column_id, "ACTIVE", "", ""),
        ]
        db.executemany(
            "INSERT INTO kb_tasks(column_id, title, archived_at, deleted_at) "
            "VALUES(?, ?, ?, ?)",
            rows,
        )
        db.commit()
    finally:
        db.close()

    return {title: (archived_at, deleted_at) for _, title, archived_at, deleted_at in rows}


def task_snapshot(app_env):
    db = sqlite3.connect(app_env.DB_PATH)
    try:
        return db.execute("SELECT * FROM kb_tasks ORDER BY id").fetchall()
    finally:
        db.close()


def test_board_get_is_read_only(app_env, client, maintenance_tasks):
    before = task_snapshot(app_env)

    response = client.get("/board")

    assert response.status_code == 200
    assert task_snapshot(app_env) == before


def test_maintenance_removes_only_trash_older_than_30_days(
    app_env,
    maintenance_tasks,
):
    assert set(maintenance_tasks) == {
        "OLD_TRASH",
        "EDGE_TRASH",
        "NEW_TRASH",
        "ARCHIVED",
        "ACTIVE",
    }
    before = task_snapshot(app_env)
    db = sqlite3.connect(app_env.DB_PATH)
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(kb_tasks)")]
    finally:
        db.close()
    title_index = columns.index("title")
    expected = [row for row in before if row[title_index] != "OLD_TRASH"]

    app_env.run_maintenance(now_utc=REFERENCE_TIME)

    assert task_snapshot(app_env) == expected
