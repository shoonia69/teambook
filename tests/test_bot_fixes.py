import asyncio
import importlib
import logging
import sqlite3
import sys

import pytest


@pytest.fixture
def bot_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_ADMIN", "111")
    db_path = tmp_path / "hr_notes.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
        CREATE TABLE departments (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
        CREATE TABLE employees (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          position_id INTEGER, department_id INTEGER, salary TEXT DEFAULT '',
          hire_date TEXT DEFAULT '', active INTEGER DEFAULT 1
        );
        CREATE TABLE problems (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
          text TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE todo_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'backlog', sort_order INTEGER NOT NULL DEFAULT 0,
          assigned_date TEXT DEFAULT '', done_date TEXT DEFAULT '',
          due_date TEXT DEFAULT '', tag TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE kb_columns (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'kanban', locked INTEGER NOT NULL DEFAULT 0,
          sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE kb_tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, column_id INTEGER,
          title TEXT NOT NULL DEFAULT '', description TEXT DEFAULT '',
          start_date TEXT DEFAULT '', due_date TEXT DEFAULT '',
          archived_at TEXT DEFAULT '', deleted_at TEXT DEFAULT ''
        );
        CREATE TABLE kb_task_members (task_id INTEGER NOT NULL, employee_id INTEGER NOT NULL);
        INSERT INTO employees (id, name) VALUES (1, 'Тест');
        """
    )
    connection.commit()
    connection.close()

    sys.modules.pop("bot_worker", None)
    module = importlib.import_module("bot_worker")
    module.DB_PATH = str(db_path)
    module.PENDING.clear()
    yield module, db_path
    module.PENDING.clear()
    sys.modules.pop("bot_worker", None)


class _Response:
    def __init__(self, text, **kwargs):
        self.text = text
        self.markup = kwargs.get("reply_markup")

    def __await__(self):
        yield
        return None


class _Message:
    def __init__(self, text):
        self.text = text
        self.reply = None

    def reply_text(self, text, **kwargs):
        self.reply = _Response(text, **kwargs)
        return self.reply


class _User:
    def __init__(self, user_id=111):
        self.id = user_id


class _Chat:
    type = "private"


class _Update:
    def __init__(self, text):
        self.message = _Message(text)
        self.effective_user = _User()
        self.effective_chat = _Chat()
        self.callback_query = None


class _Context:
    def __init__(self, args=None):
        self.args = args or []


def run_text(module, text):
    update = _Update(text)
    asyncio.run(module.on_text(update, None))
    return update.message.reply


def run_command(module, function, text, args=None):
    update = _Update(text)
    asyncio.run(function(update, _Context(args)))
    return update.message.reply


def test_todo_data_preserves_old_archive_rows(bot_env):
    module, db_path = bot_env
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO todo_items(title, status, done_date) VALUES(?, 'done', '2020-01-01')",
        ("старый арх",),
    )
    connection.commit()
    module.todo_data()
    count = connection.execute(
        "SELECT COUNT(*) FROM todo_items WHERE title=?", ("старый арх",)
    ).fetchone()[0]
    connection.close()
    assert count == 1


def test_db_enables_foreign_keys(bot_env):
    module, _ = bot_env
    connection = module.db()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_orphan_problem_is_rejected(bot_env):
    module, db_path = bot_env
    with pytest.raises(sqlite3.IntegrityError):
        module.add_employee_problem(9999, "проблема без сотрудника")
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM problems").fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize("logger_name", ["httpcore", "httpx"])
def test_http_loggers_do_not_emit_token_urls(bot_env, logger_name):
    assert logging.getLogger(logger_name).level >= logging.WARNING


def test_cancel_clears_pending_input(bot_env):
    module, _ = bot_env
    module.PENDING[111] = ("todoadd",)
    response = run_text(module, "/cancel")
    assert 111 not in module.PENDING
    assert response.text.startswith("Отменено")


def test_month_command_is_routed_instead_of_saved_as_todo(bot_env):
    module, db_path = bot_env
    module.PENDING[111] = ("todoadd",)
    response = run_text(module, "/m12")
    assert "Выберите столбец" in response.text
    assert 111 not in module.PENDING
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM todo_items WHERE title='m12'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_plain_text_completes_pending_todo_input(bot_env):
    module, db_path = bot_env
    module.PENDING[111] = ("todoadd",)
    run_text(module, "обычный текст")
    assert 111 not in module.PENDING
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM todo_items WHERE title=?", ("обычный текст",)
        ).fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("function_name", "text", "args"),
    [
        ("cmd_start", "/start", []),
        ("cmd_help", "/help", []),
        ("cmd_m", "/m 12", ["12"]),
    ],
)
def test_navigation_commands_clear_pending_input(bot_env, function_name, text, args):
    module, _ = bot_env
    module.PENDING[111] = ("todoadd",)
    run_command(module, getattr(module, function_name), text, args)
    assert 111 not in module.PENDING


def test_unknown_command_clears_pending_and_reports_error(bot_env):
    module, _ = bot_env
    module.PENDING[111] = ("todoadd",)
    response = run_text(module, "/zzz")
    assert 111 not in module.PENDING
    assert "Не знаю такой команды" in response.text


def test_cap_truncates_long_text(bot_env):
    module, _ = bot_env
    result = module._cap("x" * 5000, 4000)
    assert len(result) <= 4000


def test_cap_preserves_short_text(bot_env):
    module, _ = bot_env
    assert module._cap("ок", 4000) == "ок"
