import os
import sqlite3

import pytest


EXPECTED_LEDGER = [
    (1, "baseline"),
    (2, "versioned-runner"),
    (3, "drop-legacy-space-owner"),
]


def read_ledger(path):
    db = sqlite3.connect(path)
    try:
        return db.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        db.close()


def test_schema_ledger_is_created_and_init_is_idempotent(app_env):
    assert read_ledger(app_env.DB_PATH) == EXPECTED_LEDGER

    app_env.init_db()

    assert read_ledger(app_env.DB_PATH) == EXPECTED_LEDGER


def test_reapplying_migrations_does_not_change_ledger(app_env):
    db = sqlite3.connect(app_env.DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        before = [
            tuple(row)
            for row in db.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )
        ]
        app_env._apply_migrations(db)
        db.commit()
        after = [
            tuple(row)
            for row in db.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )
        ]
    finally:
        db.close()

    assert before == EXPECTED_LEDGER
    assert after == before


def test_legacy_database_without_ledger_is_upgraded(app_env, tmp_path):
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    legacy_db = legacy_dir / "hr_notes.db"
    db = sqlite3.connect(legacy_db)
    try:
        db.executescript(app_env.SCHEMA)
        db.execute("DROP TABLE schema_migrations")
        db.execute("INSERT INTO employees(name) VALUES(?)", ("Legacy",))
        db.commit()
    finally:
        db.close()

    app_env.DATA_DIR = str(legacy_dir)
    app_env.DB_PATH = str(legacy_db)
    app_env.init_db()

    db = sqlite3.connect(legacy_db)
    try:
        employee = db.execute("SELECT name FROM employees").fetchone()[0]
        ledger = db.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        db.close()

    assert employee == "Legacy"
    assert ledger == EXPECTED_LEDGER


@pytest.mark.parametrize(
    "forged_rows",
    [
        [(1, "attacker")],
        [(2, "baseline")],
        [(1, "baseline"), (3, "future")],
    ],
    ids=["conflicting-name", "wrong-version", "future-version"],
)
def test_forged_migration_ledgers_fail_closed(app_env, tmp_path, forged_rows):
    bad_dir = tmp_path / "forged"
    bad_dir.mkdir()
    bad_db = bad_dir / "hr_notes.db"
    db = sqlite3.connect(bad_db)
    try:
        db.executescript(app_env.SCHEMA)
        db.execute("DELETE FROM schema_migrations")
        db.executemany(
            "INSERT INTO schema_migrations(version, name) VALUES(?, ?)",
            forged_rows,
        )
        db.commit()
    finally:
        db.close()

    app_env.DATA_DIR = str(bad_dir)
    app_env.DB_PATH = str(bad_db)

    with pytest.raises(RuntimeError) as exc_info:
        app_env.init_db()

    assert "schema_migrations" in str(exc_info.value) or "Неизвестная миграция" in str(
        exc_info.value
    )
    assert read_ledger(bad_db) == forged_rows
