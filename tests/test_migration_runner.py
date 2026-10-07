import sqlite3

import pytest


EXPECTED_LEDGER = [(1, "baseline"), (2, "versioned-runner")]


def ledger(db):
    return [
        tuple(row)
        for row in db.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        )
    ]


def object_exists(db, object_type, name):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
            (object_type, name),
        ).fetchone()
        is not None
    )


def test_initial_versioned_migration_ledger_is_exact(app_env):
    db = app_env._connect_db()
    try:
        assert ledger(db) == EXPECTED_LEDGER
    finally:
        db.close()


def test_migration_callback_runs_exactly_once(app_env):
    calls = []

    def migration(db):
        calls.append(1)
        db.execute("CREATE TABLE runner_probe(id INTEGER)")

    db = app_env._connect_db()
    try:
        app_env._run_versioned_migrations(db, ((3, "probe", migration),))
        db.commit()
    finally:
        db.close()

    db = app_env._connect_db()
    try:
        app_env._run_versioned_migrations(db, ((3, "probe", migration),))
        db.commit()
        assert calls == [1]
        assert ledger(db)[-1] == (3, "probe")
        assert object_exists(db, "table", "runner_probe")
    finally:
        db.close()


def test_failing_migration_rolls_back_body_and_ledger(app_env):
    def broken(db):
        db.execute("CREATE TABLE broken_probe(id INTEGER)")
        raise RuntimeError("boom")

    db = app_env._connect_db()
    try:
        with pytest.raises(RuntimeError, match="boom"):
            app_env._run_versioned_migrations(db, ((3, "broken", broken),))

        assert not object_exists(db, "table", "broken_probe")
        assert db.execute(
            "SELECT 1 FROM schema_migrations WHERE version=3"
        ).fetchone() is None
    finally:
        db.rollback()
        db.close()

    verify = app_env._connect_db()
    try:
        assert not object_exists(verify, "table", "broken_probe")
        assert ledger(verify) == EXPECTED_LEDGER
    finally:
        verify.close()


def test_executescript_then_failure_is_rejected_without_ledger_stamp(app_env):
    def scripted(db):
        db.executescript(
            "CREATE TABLE scripted_probe(id INTEGER); "
            "INSERT INTO scripted_probe VALUES(1);"
        )
        raise RuntimeError("boom")

    db = app_env._connect_db()
    try:
        with pytest.raises(RuntimeError, match="executescript"):
            app_env._run_versioned_migrations(db, ((3, "scripted", scripted),))

        assert db.execute(
            "SELECT 1 FROM schema_migrations WHERE version=3"
        ).fetchone() is None
        assert object_exists(db, "table", "scripted_probe")
    finally:
        db.close()

    verify = app_env._connect_db()
    try:
        assert ledger(verify) == EXPECTED_LEDGER
        assert object_exists(verify, "table", "scripted_probe")
    finally:
        verify.close()


def test_successful_executescript_is_rejected_without_ledger_stamp(app_env):
    def scripted_ok(db):
        db.executescript(
            "CREATE TABLE scripted_ok_probe(id INTEGER); "
            "INSERT INTO scripted_ok_probe VALUES(1);"
        )

    db = app_env._connect_db()
    try:
        with pytest.raises(RuntimeError, match="executescript"):
            app_env._run_versioned_migrations(
                db,
                ((3, "scripted-ok", scripted_ok),),
            )

        assert db.execute(
            "SELECT 1 FROM schema_migrations WHERE version=3"
        ).fetchone() is None
        assert object_exists(db, "table", "scripted_ok_probe")
    finally:
        db.close()

    verify = app_env._connect_db()
    try:
        assert ledger(verify) == EXPECTED_LEDGER
        assert object_exists(verify, "table", "scripted_ok_probe")
    finally:
        verify.close()
