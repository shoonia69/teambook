from pathlib import Path


def test_web_sqlite_connection_uses_wal_and_busy_timeout(app_env):
    with app_env.app.app_context():
        db = app_env.get_db()
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000


def test_wsgi_is_import_only():
    source = Path("wsgi.py").read_text(encoding="utf-8")
    assert "init_db()" not in source


def test_entrypoint_initializes_before_processes_and_owns_children():
    source = Path("entrypoint.sh").read_text(encoding="utf-8")
    assert "app.init_db()" in source
    assert "trap 'shutdown'" in source or 'trap "shutdown"' in source
    assert "BOT_PID=$!" in source
    assert 'kill -0 "$WEB_PID"' in source
    assert "shutdown\n    python /app/restore_offline.py" in source
    assert ".restore-fatal" in source
    assert "exit 76" in source


def test_bot_supervisor_lifecycle_contracts():
    source = Path("bot.py").read_text(encoding="utf-8")
    assert "signal.SIGTERM" in source
    assert "logfile.close()" in source
    assert "BACKOFF_MAX" in source
    assert "backoff" in source
    assert "MAINTENANCE_LOCK" in source


def test_bot_worker_sqlite_has_busy_timeout():
    source = Path("bot_worker.py").read_text(encoding="utf-8")
    assert "busy_timeout" in source


def test_restore_publication_and_unique_backup_helpers_exist(app_env):
    assert hasattr(app_env, "RESTORE_REQUEST")
    assert hasattr(app_env, "_unique_restore_backup_path")


def test_offline_restore_validates_and_can_roll_back():
    source = Path("restore_offline.py").read_text(encoding="utf-8")
    assert "NAME_RE.fullmatch" in source
    assert "os.lstat" in source
    assert "os.replace(backup, CURRENT)" in source
    assert "_quarantine" in source
    assert "_validate_db_schema" in source
    assert "_remove_sidecars(CURRENT)" in source


def test_offline_restore_fsyncs_writable_files_and_tracks_rollback():
    source = Path("restore_offline.py").read_text(encoding="utf-8")
    assert "os.open(CURRENT, os.O_RDWR)" in source
    assert "os.open(backup, os.O_RDWR)" in source
    assert "fatal = swapped and not rollback_ok" in source
    assert "rollback_ok = True" in source
