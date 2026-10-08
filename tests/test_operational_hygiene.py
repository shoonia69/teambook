import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_health_exposes_only_status_and_commit(app_env, monkeypatch):
    monkeypatch.setattr(app_env, "COMMIT_SHA", "abc123def456")
    response = app_env.app.test_client().get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "commit": "abc123def456"}
    body = response.get_data(as_text=True)
    assert "test-password" not in body
    assert "test-secret-key" not in body


def test_health_uses_unknown_for_missing_build_sha(app_env, monkeypatch):
    monkeypatch.setattr(app_env, "COMMIT_SHA", "")
    assert app_env.app.test_client().get("/healthz").get_json()["commit"] == "unknown"


def test_dockerfile_embeds_build_sha_and_has_healthcheck():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG COMMIT_SHA=unknown" in text
    assert "ENV COMMIT_SHA=$COMMIT_SHA" in text
    assert "HEALTHCHECK" in text
    assert "http://127.0.0.1:5000/healthz" in text


def test_deploy_script_requires_and_passes_commit_sha():
    text = (ROOT / "scripts" / "build-image.sh").read_text(encoding="utf-8")
    assert '${COMMIT_SHA:?set COMMIT_SHA to the immutable git revision}' in text
    assert '--build-arg "COMMIT_SHA=$COMMIT_SHA"' in text
    assert '"$IMAGE:$COMMIT_SHA"' in text


def test_retention_removes_only_expired_operational_artifacts(app_env, tmp_path):
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    expired = now - timedelta(days=31)
    edge = now - timedelta(days=30)
    names = [
        "hr_notes.db.pre_restore_old.bak",
        ".restore-rejected-old.json",
        ".restore-rejected-old.json.db",
        ".restore-rejected-old.json.error",
    ]
    edge_name = "hr_notes.db.pre_restore_edge.bak"
    unrelated = "employee-export.db"
    for name in [*names, edge_name, unrelated]:
        path = tmp_path / name
        path.write_text(name, encoding="utf-8")
        stamp = expired.timestamp() if name in names else edge.timestamp()
        os.utime(path, (stamp, stamp))

    app_env.run_maintenance(now_utc=now)

    assert all(not (tmp_path / name).exists() for name in names)
    assert (tmp_path / edge_name).exists()
    assert (tmp_path / unrelated).exists()


def test_maintenance_checkpoints_wal(app_env, monkeypatch):
    seen = []
    real_connect = app_env._connect_db

    class RecordingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            seen.append(sql)
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(app_env, "_connect_db", lambda path=None: RecordingConnection(real_connect(path)))

    app_env.run_maintenance(now_utc=datetime(2026, 10, 8, 12, tzinfo=timezone.utc))

    assert "PRAGMA wal_checkpoint(TRUNCATE)" in seen


def test_maintenance_rejects_low_free_space_before_checkpoint(app_env, monkeypatch):
    monkeypatch.setenv("HR_MIN_FREE_BYTES", "1000")
    monkeypatch.setattr(app_env.shutil, "disk_usage", lambda _path: (2000, 1501, 499))

    with pytest.raises(RuntimeError, match="free disk space"):
        app_env.run_maintenance()


def test_entrypoint_keeps_restore_before_maintenance_and_writers():
    text = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    restore = text.index("python /app/restore_offline.py")
    maintenance = text.index("app.run_maintenance()")
    bot = text.index("python /app/bot.py")
    web = text.index("gunicorn")
    assert restore < maintenance < bot < web


def test_cleanup_migration_drops_only_confirmed_space_owner_columns(app_env, tmp_path):
    legacy_dir = tmp_path / "legacy-space-columns"
    legacy_dir.mkdir()
    legacy_db = legacy_dir / "hr_notes.db"
    source = sqlite3.connect(app_env.DB_PATH)
    target = sqlite3.connect(legacy_db)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    with sqlite3.connect(legacy_db) as db:
        for table in ("employee_history", "meetings", "problems"):
            db.execute(
                f"ALTER TABLE {table} ADD COLUMN "
                "space_owner INTEGER NOT NULL DEFAULT 0"
            )
        employee_id = db.execute(
            "INSERT INTO employees(name) VALUES(?) RETURNING id",
            ("Legacy Space Employee",),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO meetings(employee_id, date, summary, space_owner) "
            "VALUES(?, ?, ?, 1)",
            (employee_id, "2026-10-08", "Preserved meeting"),
        )
        db.execute("DELETE FROM schema_migrations WHERE version=3")
        db.commit()

    with sqlite3.connect(legacy_db) as db:
        before = {
            table: {
                "rows": db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall(),
                "columns": [
                    row[1] for row in db.execute(f"PRAGMA table_info({table})")
                ],
                "fks": db.execute(f"PRAGMA foreign_key_list({table})").fetchall(),
                "indexes": db.execute(f"PRAGMA index_list({table})").fetchall(),
            }
            for table in ("employee_history", "meetings", "problems")
        }

    app_env.DATA_DIR = str(legacy_dir)
    app_env.DB_PATH = str(legacy_db)
    app_env.init_db()

    with sqlite3.connect(legacy_db) as db:
        for table in ("employee_history", "meetings", "problems"):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            assert "space_owner" not in columns
            after_rows = db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            legacy_index = before[table]["columns"].index("space_owner")
            expected_rows = [
                tuple(value for index, value in enumerate(row) if index != legacy_index)
                for row in before[table]["rows"]
            ]
            assert after_rows == expected_rows
            assert db.execute(f"PRAGMA foreign_key_list({table})").fetchall() == before[table]["fks"]
            assert db.execute(f"PRAGMA index_list({table})").fetchall() == before[table]["indexes"]
        assert db.execute(
            "SELECT summary FROM meetings WHERE employee_id=?",
            (employee_id,),
        ).fetchone() == ("Preserved meeting",)
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
