# -*- coding: utf-8 -*-
"""
TeamBook — блокнот руководителя.
Заметки о подчинённых: карточки сотрудников (должность/отдел/зарплата),
справочники должностей и отделов, полугодовые записи (цели, предложения,
пожелания, комментарии), итоги встреч 1-на-1. Разбивка по годам.
"""

import os
import sqlite3
import secrets
import time
import shutil
import tempfile
import re
from contextlib import contextmanager
from datetime import datetime, date, timedelta, timezone
from functools import wraps

from flask import (
    Flask, request, redirect, url_for, render_template, session, flash, abort, g,
    send_file,
)

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    HAS_EXCEL = True
except Exception:
    HAS_EXCEL = False

try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    HAS_PDF = True
except Exception:
    HAS_PDF = False

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("HR_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "hr_notes.db")
MAINTENANCE_LOCK = os.path.join(DATA_DIR, ".maintenance.lock")
RESTORE_REQUEST = os.path.join(DATA_DIR, ".restore-request.json")
COMMIT_SHA = os.environ.get("COMMIT_SHA", "unknown").strip()
OPERATIONAL_RETENTION_DAYS = int(os.environ.get("HR_OPERATIONAL_RETENTION_DAYS", "30"))
MIN_FREE_BYTES = 64 * 1024 * 1024

ADMIN_PASSWORD = os.environ.get("HR_PASSWORD", "")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("HR_SECRET_KEY", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("HR_COOKIE_SECURE", "0") == "1",
    MAX_CONTENT_LENGTH=int(os.environ.get("HR_MAX_UPLOAD_BYTES", 32 * 1024 * 1024)),
)

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60


def _login_rate_limit(ip, success=False, register_failure=False):
    """Атомарно проверяет/обновляет общий rate-limit всех Gunicorn workers."""
    now = int(time.time())
    cutoff = now - LOGIN_WINDOW_SECONDS
    con = sqlite3.connect(DB_PATH, timeout=5)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM login_failures WHERE failed_at < ?", (cutoff,))
        count = con.execute(
            "SELECT COUNT(*) FROM login_failures WHERE ip=? AND failed_at>=?",
            (ip, cutoff),
        ).fetchone()[0]
        blocked = count >= LOGIN_MAX_FAILURES
        if success:
            if not blocked:
                con.execute("DELETE FROM login_failures WHERE ip=?", (ip,))
            con.commit()
            return blocked
        if blocked:
            con.commit()
            return True
        if register_failure:
            con.execute("INSERT INTO login_failures(ip, failed_at) VALUES (?,?)", (ip, now))
            count += 1
        con.commit()
        return count >= LOGIN_MAX_FAILURES
    except sqlite3.OperationalError:
        # При lock/timeout не открываем обход защиты и не отдаём 500.
        try:
            con.rollback()
        except Exception:
            pass
        return True
    finally:
        con.close()

SEMESTERS = {"1H": "I полугодие (янв–июн)", "2H": "II полугодие (июл–дек)"}

# --------------------------------------------------------------------------- #
# БД
# --------------------------------------------------------------------------- #
def _connect_db(path=None):
    path = path or DB_PATH
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    if path == DB_PATH:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
    return db


def get_db():
    if "db" not in g:
        g.db = _connect_db()
    return g.db


@contextmanager
def _maintenance_lock():
    """Атомарный lock для операций без онлайн-замены активной БД."""
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        fd = os.open(MAINTENANCE_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Файл живёт в container volume: после аварийного рестарта старый PID уже
        # не может владеть им, поэтому entrypoint очищает его до запуска процессов.
        raise RuntimeError("обслуживание базы уже выполняется")
    try:
        os.write(fd, str(os.getpid()).encode("ascii")); os.close(fd)
        yield
    finally:
        try: os.remove(MAINTENANCE_LOCK)
        except FileNotFoundError: pass


def _unique_restore_backup_path():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return DB_PATH + ".pre_restore_" + stamp + ".bak"


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS departments (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS employees (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    position_id   INTEGER REFERENCES positions(id) ON DELETE SET NULL,
    department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    salary        TEXT DEFAULT '',
    hire_date     TEXT DEFAULT '',
    notes         TEXT DEFAULT '',
    active        INTEGER DEFAULT 1,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS employee_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    change_date TEXT NOT NULL,
    position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL,
    salary      TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS year_records (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id       INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    year              INTEGER NOT NULL,
    semester          TEXT NOT NULL CONSTRAINT ck_year_records_semester
                      CHECK (semester IN ('1H', '2H')),
    goals_employee    TEXT DEFAULT '',
    proposals_manager TEXT DEFAULT '',
    wishes_employee   TEXT DEFAULT '',
    comments          TEXT DEFAULT '',
    colleagues_feedback TEXT DEFAULT '',
    updated_at        TEXT DEFAULT (datetime('now')),
    UNIQUE(employee_id, year, semester)
);

CREATE TABLE IF NOT EXISTS meetings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    date        TEXT NOT NULL,
    summary     TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tags (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL UNIQUE,
    color  TEXT NOT NULL DEFAULT '#5B8DEF',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS employee_tags (
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (employee_id, tag_id)
);

CREATE TABLE IF NOT EXISTS problems (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    text        TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Канбан-доска и гант (задачи и сроки)

-- Столбцы канбана (свободные, создаются руководителем)
-- kind: 'kanban' = обычный столбец канбана
-- locked=1 = системный столбец (не создаётся; Бэклог остался только в личном todo)
CREATE TABLE IF NOT EXISTS kb_columns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'kanban',
    locked      INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Задачи: исполнители (employee, может быть несколько) + сроки для ганта.
CREATE TABLE IF NOT EXISTS kb_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    column_id    INTEGER REFERENCES kb_columns(id) ON DELETE SET NULL,
    title        TEXT NOT NULL DEFAULT '',
    description  TEXT DEFAULT '',
    start_date   TEXT NOT NULL DEFAULT '' CONSTRAINT ck_kb_tasks_start_date
                 CHECK (start_date = '' OR date(start_date) = start_date),
    due_date     TEXT NOT NULL DEFAULT '' CONSTRAINT ck_kb_tasks_due_date
                 CHECK (due_date = '' OR date(due_date) = due_date),
    archived_at  TEXT DEFAULT '',   -- не пусто = в архиве
    deleted_at   TEXT DEFAULT '',   -- не пусто = в корзине
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now')),
    CONSTRAINT ck_kb_tasks_date_order
    CHECK (start_date = '' OR due_date = '' OR start_date <= due_date)
);

-- Исполнители задачи (many-to-many: у задачи может быть несколько сотрудников)
CREATE TABLE IF NOT EXISTS kb_task_members (
    task_id     INTEGER NOT NULL REFERENCES kb_tasks(id) ON DELETE CASCADE,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, employee_id)
);

-- Настройки (key-value): Telegram-бот, резервная копия и пр.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Общий межпроцессный rate-limit входа (Gunicorn workers разделяют SQLite).
CREATE TABLE IF NOT EXISTS login_failures (
    ip        TEXT NOT NULL,
    failed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Личный todo руководителя: бэклог задач + матрица Эйзенхауэра (важно×срочно).
-- status: 'backlog' (вход для новых) | квадранты 'q_iu'/'q_in'/'q_nu'/'q_nn'
--         | 'done' (архив: выполненные за сегодня)
--   q_iu = важно + срочно, q_in = важно + не срочно,
--   q_nu = не важно + срочно, q_nn = не важно + не срочно
-- assigned_date: резерв (не используется с 2026, квадранты вместо «на сегодня»)
-- due_date: опциональный срок (для подсветки просрочки и уведомлений в шапке)
-- tag: необязательный тег/группа («серверная», «люди», «1-1» и т.п.)
CREATE TABLE IF NOT EXISTS todo_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'backlog' CONSTRAINT ck_todo_status
                  CHECK (status IN ('backlog','q_iu','q_in','q_nu','q_nn','done')),
    sort_order    INTEGER NOT NULL DEFAULT 0,
    assigned_date TEXT DEFAULT '',
    done_date     TEXT NOT NULL DEFAULT '' CONSTRAINT ck_todo_done_date
                  CHECK (done_date = '' OR date(done_date) = done_date),
    due_date      TEXT NOT NULL DEFAULT '' CONSTRAINT ck_todo_due_date
                  CHECK (due_date = '' OR date(due_date) = due_date),
    tag           TEXT DEFAULT '',
    created_at    TEXT DEFAULT (datetime('now')),
    CONSTRAINT ck_todo_done_state
    CHECK ((status = 'done' AND done_date != '') OR (status != 'done' AND done_date = ''))
);

-- Прикладные индексы для наиболее частых списков, счётчиков и уведомлений.
"""

INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_login_failures_ip_time ON login_failures(ip, failed_at);
CREATE INDEX IF NOT EXISTS idx_employee_history_employee_date ON employee_history(employee_id, change_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_meetings_employee_date ON meetings(employee_id, date DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_problems_employee_id ON problems(employee_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_year_records_year ON year_records(year);
CREATE INDEX IF NOT EXISTS idx_kb_columns_kind_order ON kb_columns(kind, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_kb_tasks_live ON kb_tasks(id DESC) WHERE archived_at = '' AND deleted_at = '';
CREATE INDEX IF NOT EXISTS idx_kb_tasks_live_due ON kb_tasks(due_date, id) WHERE archived_at = '' AND deleted_at = '' AND due_date != '';
CREATE INDEX IF NOT EXISTS idx_kb_tasks_deleted ON kb_tasks(deleted_at DESC, id DESC) WHERE deleted_at != '';
CREATE INDEX IF NOT EXISTS idx_kb_tasks_archived ON kb_tasks(archived_at DESC, id DESC) WHERE archived_at != '' AND deleted_at = '';

CREATE INDEX IF NOT EXISTS idx_todo_open_due ON todo_items(due_date, id) WHERE status != 'done' AND due_date != '';
CREATE INDEX IF NOT EXISTS idx_todo_status_order ON todo_items(status, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_todo_done_date ON todo_items(done_date, id) WHERE status = 'done';
"""


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = _connect_db()
    _apply_migrations(db)
    db.commit()
    db.close()


def run_maintenance(now_utc=None):
    """Запускает обслуживающие операции вне HTTP GET-запросов."""
    required_free = int(os.environ.get("HR_MIN_FREE_BYTES", str(MIN_FREE_BYTES)))
    free = shutil.disk_usage(DATA_DIR)[2]
    if free < required_free:
        raise RuntimeError(
            "insufficient free disk space: %d bytes available, %d required"
            % (free, required_free)
        )
    db = _connect_db()
    try:
        _purge_stale_trash(db, now_utc=now_utc)
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    _purge_operational_artifacts(now_utc=now_utc)


def _purge_operational_artifacts(now_utc=None):
    """Удаляет только известные backup/quarantine artifacts старше retention."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now.timestamp() - OPERATIONAL_RETENTION_DAYS * 86400
    prefixes = ("hr_notes.db.pre_restore_", ".restore-rejected-")
    for entry in os.scandir(DATA_DIR):
        if not entry.is_file(follow_symlinks=False):
            continue
        if not entry.name.startswith(prefixes):
            continue
        if entry.stat(follow_symlinks=False).st_mtime < cutoff:
            os.remove(entry.path)


REQUIRED_CHECKS = {
    "year_records": {
        "ck_year_records_semester": "semesterin('1h','2h'",
    },
    "todo_items": {
        "ck_todo_status": "statusin('backlog','q_iu','q_in','q_nu','q_nn','done'",
        "ck_todo_done_date": "done_date=''ordate(done_date)=done_date",
        "ck_todo_due_date": "due_date=''ordate(due_date)=due_date",
        "ck_todo_done_state":
            "(status='done'anddone_date!='')or(status!='done'anddone_date=''",
    },
    "kb_tasks": {
        "ck_kb_tasks_start_date": "start_date=''ordate(start_date)=start_date",
        "ck_kb_tasks_due_date": "due_date=''ordate(due_date)=due_date",
        "ck_kb_tasks_date_order":
            "start_date=''ordue_date=''orstart_date<=due_date",
    },
}


def _normalized_check_contracts(ddl):
    compact = re.sub(r"\s+", "", (ddl or "").lower())
    found = {}
    for name, expression in re.findall(
            r"constraint([a-z0-9_]+)check\((.*?)\)(?=,|\)|$)", compact):
        found.setdefault(name, []).append(expression)
    return found


def _table_has_required_checks(db, table):
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    actual = _normalized_check_contracts(row[0] if row else "")
    return all(actual.get(name) == [expr]
               for name, expr in REQUIRED_CHECKS[table].items())


def _rebuild_constrained_tables(db):
    """Пересоздаёт legacy-таблицы, чтобы CHECK применялись к существующим БД."""
    for table in ("year_records", "todo_items", "kb_tasks"):
        if _table_has_required_checks(db, table):
            continue
        if table == "year_records":
            unique_cols = [tuple(r[2] for r in db.execute(
                f"PRAGMA index_info('{idx[1]}')").fetchall())
                for idx in db.execute("PRAGMA index_list('year_records')").fetchall()
                if idx[2]]
            if ("employee_id", "year", "semester") not in unique_cols:
                continue
        if table == "kb_tasks":
            fk_ok = any(
                r[2] == "kb_columns" and r[3] == "column_id" and r[4] == "id"
                for r in db.execute("PRAGMA foreign_key_list('kb_tasks')").fetchall()
            )
            if not fk_ok:
                continue
        match = re.search(
            rf"CREATE TABLE IF NOT EXISTS\s+{re.escape(table)}\s*\(.*?\n\);",
            SCHEMA,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            raise RuntimeError(f"CREATE TABLE для {table} не найден в SCHEMA")
        target_sql = match.group(0).rstrip(";")
        temp = table + "__constrained"
        db.execute("PRAGMA legacy_alter_table=ON")
        try:
            db.execute(f"DROP TABLE IF EXISTS {temp}")
            db.execute(target_sql.replace(f"IF NOT EXISTS {table}", temp, 1))
            source_cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            target_cols = [r[1] for r in db.execute(f"PRAGMA table_info({temp})")]
            cols = [c for c in target_cols if c in source_cols]
            names = ",".join(f'"{c}"' for c in cols)
            db.execute(f"INSERT INTO {temp} ({names}) SELECT {names} FROM {table}")
            db.execute(f"DROP TABLE {table}")
            db.execute(f"ALTER TABLE {temp} RENAME TO {table}")
        finally:
            db.execute("PRAGMA legacy_alter_table=OFF")
        print(f"[TeamBook] Миграция {table}: установлены CHECK-ограничения")


def _apply_migrations(db):
    """Создаёт/приводит схему к актуальному виду. Вызывается и при старте, и перед
    активацией восстанавливаемой БД (backup/import) — чтобы импортированный файл
    старой схемы не ломал приложение (например /todo при отсутствии колонки tag)."""
    db.executescript(SCHEMA)

    # Миграция со старой схемы: отвлекаемся на employees с TEXT position/department.
    cols = {r[1] for r in db.execute("PRAGMA table_info(employees)").fetchall()}
    if "position" in cols and "position_id" not in cols:
        _migrate_employees(db)

    # Миграция: добавление колонки notes (общие заметки) к уже существующим БД
    cols = {r[1] for r in db.execute("PRAGMA table_info(employees)").fetchall()}
    if "notes" not in cols:
        db.execute("ALTER TABLE employees ADD COLUMN notes TEXT DEFAULT ''")
        print("[TeamBook] Миграция employees: добавлена колонка notes")

    # Миграция: добавление колонки hire_date (дата приёма)
    cols = {r[1] for r in db.execute("PRAGMA table_info(employees)").fetchall()}
    if "hire_date" not in cols:
        db.execute("ALTER TABLE employees ADD COLUMN hire_date TEXT DEFAULT ''")
        print("[TeamBook] Миграция employees: добавлена колонка hire_date")

    # Миграция: добавление колонки colleagues_feedback (отзывы коллег)
    cols = {r[1] for r in db.execute("PRAGMA table_info(year_records)").fetchall()}
    if "colleagues_feedback" not in cols:
        db.execute("ALTER TABLE year_records ADD COLUMN colleagues_feedback TEXT DEFAULT ''")
        print("[TeamBook] Миграция year_records: добавлена колонка colleagues_feedback")

    # Починка FK-ссылок, сломанных переименованием employees (см. _migrate_employees).
    # SQLite при ALTER TABLE RENAME переписывает ссылки на employees в дочерние
    # таблицы (meetings, year_records) -> employees_old, которые после DROP битые.
    _repair_dangling_fk(db, "meetings", "employee_id")
    _repair_dangling_fk(db, "year_records", "employee_id")

    # миграция todo: колонка done_date (для архива выполненных сегодня)
    todo_cols = {r[1] for r in db.execute("PRAGMA table_info(todo_items)").fetchall()}
    if "done_date" not in todo_cols:
        db.execute("ALTER TABLE todo_items ADD COLUMN done_date TEXT DEFAULT ''")
    # миграция: прежний статус 'today' («на сегодня») -> квадрант «важно + срочно»
    db.execute("UPDATE todo_items SET status='q_iu' WHERE status='today'")
    # миграция: срок и тег в личном todo
    if "due_date" not in todo_cols:
        db.execute("ALTER TABLE todo_items ADD COLUMN due_date TEXT DEFAULT ''")
    if "tag" not in todo_cols:
        db.execute("ALTER TABLE todo_items ADD COLUMN tag TEXT DEFAULT ''")

    # Нормализуем legacy-значения до установки CHECK-ограничений.
    db.execute("UPDATE todo_items SET status='backlog' WHERE status NOT IN "
               "('backlog','q_iu','q_in','q_nu','q_nn','done')")
    db.execute("UPDATE todo_items SET done_date='' WHERE status!='done'")
    db.execute("UPDATE todo_items SET done_date=date('now') WHERE status='done' "
               "AND (done_date='' OR date(done_date)!=done_date)")
    db.execute("UPDATE todo_items SET due_date='' WHERE due_date!='' "
               "AND (date(due_date) IS NULL OR date(due_date)!=due_date)")

    db.execute("UPDATE year_records SET semester='1H' WHERE semester NOT IN ('1H','2H')")

    # Миграция канбана: колонки архива/корзины + many-to-many исполнители.
    kb_cols = {r[1] for r in db.execute("PRAGMA table_info(kb_tasks)").fetchall()}
    if "archived_at" not in kb_cols:
        db.execute("ALTER TABLE kb_tasks ADD COLUMN archived_at TEXT DEFAULT ''")
    if "deleted_at" not in kb_cols:
        db.execute("ALTER TABLE kb_tasks ADD COLUMN deleted_at TEXT DEFAULT ''")
    db.execute("UPDATE kb_tasks SET start_date='' WHERE start_date!='' "
               "AND (date(start_date) IS NULL OR date(start_date)!=start_date)")
    db.execute("UPDATE kb_tasks SET due_date='' WHERE due_date!='' "
               "AND (date(due_date) IS NULL OR date(due_date)!=due_date)")
    db.execute("UPDATE kb_tasks SET start_date='' WHERE start_date!='' "
               "AND due_date!='' AND start_date>due_date")
    db.execute("""CREATE TABLE IF NOT EXISTS kb_task_members (
        task_id     INTEGER NOT NULL REFERENCES kb_tasks(id) ON DELETE CASCADE,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        PRIMARY KEY (task_id, employee_id)
    )""")
    # перенос старого единственного исполнителя (employee_id) в kb_task_members
    if "employee_id" in kb_cols:
        n = db.execute(
            """INSERT OR IGNORE INTO kb_task_members (task_id, employee_id)
               SELECT id, employee_id FROM kb_tasks
               WHERE employee_id IS NOT NULL AND employee_id != 0"""
        ).rowcount
        if n:
            print(f"[TeamBook] Миграция канбана: перенесено исполнителей -> {n}")
        try:
            # колонка employee_id больше не нужна (исполнители в kb_task_members)
            db.execute("ALTER TABLE kb_tasks DROP COLUMN employee_id")
        except Exception:
            pass

    # --- Столбцы канбана: никаких системных колонок не создаём ---
    # Бэклог остался только в личном todo руководителя.
    kcols = {r[1] for r in db.execute("PRAGMA table_info(kb_columns)").fetchall()}
    if "kind" not in kcols:
        db.execute("ALTER TABLE kb_columns ADD COLUMN kind TEXT NOT NULL DEFAULT 'kanban'")
    if "locked" not in kcols:
        db.execute("ALTER TABLE kb_columns ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")

    # Убираем остатки системного 📥 Бэклога с канбана (если он остался с прошлых
    # версий и является системной колонкой): задачи переносим в первый обычный
    # столбец, а саму колонку удаляем. Если обычных столбцов нет — Бэклог просто
    # становится обычным пользовательским столбцом (можно удалить вручную).
    sys_backlog = db.execute(
        "SELECT id FROM kb_columns WHERE kind='kanban' AND locked=1 LIMIT 1").fetchone()
    if sys_backlog:
        target = db.execute(
            "SELECT id FROM kb_columns WHERE kind='kanban' AND locked=0 "
            "ORDER BY sort_order, id LIMIT 1").fetchone()
        if target:
            # переносим ВСЕ связанные задачи (в т.ч. архивные/удалённые), иначе
            # при удалении столбца FK выставит им column_id=NULL и карточки
            # «потеряются» при восстановлении
            db.execute(
                "UPDATE kb_tasks SET column_id=? WHERE column_id=?",
                (target["id"], sys_backlog["id"]))
            db.execute("DELETE FROM kb_columns WHERE id=?", (sys_backlog["id"],))
            print("[TeamBook] Системный 📥 Бэклог удалён с канбана, задачи перенесены")
        else:
            db.execute("UPDATE kb_columns SET locked=0, sort_order=0 WHERE id=?",
                       (sys_backlog["id"],))
            print("[TeamBook] Других столбцов нет — 📥 Бэклог стал обычным столбцом")

    # Задачи без столбца (старая схема) -> в первый обычный столбец; если столбцов
    # нет совсем, оставляем column_id=NULL (доска их не прячет).
    first_col = db.execute(
        "SELECT id FROM kb_columns WHERE kind='kanban' ORDER BY sort_order, id LIMIT 1"
    ).fetchone()
    if first_col:
        db.execute("UPDATE kb_tasks SET column_id=? WHERE column_id IS NULL",
                   (first_col["id"],))

    # Откат матрицы Эйзенхауэра: если с прошлого деплоя остались квадранты-колонки
    # (kind='emi1'..'emi4'), убираем их. Сначала переносим ВСЕ их задачи (активные,
    # архивные и удалённые) в первый обычный столбец — иначе удаление сломает FK.
    emi_cols = db.execute(
        "SELECT id FROM kb_columns WHERE kind LIKE 'emi%'").fetchall()
    if emi_cols:
        target = db.execute(
            "SELECT id FROM kb_columns WHERE kind='kanban' ORDER BY sort_order, id LIMIT 1"
        ).fetchone()
        emi_target = target["id"] if target else None
        if emi_target is None:
            # emi-only база: обычного kanban-столбца нет. Создаём приёмник ЗАРАНЕЕ,
            # иначе после удаления emi-квадрантов их задачи останутся с битым FK
            # (column_id ссылается на удалённый столбец) и foreign_key_check завалится.
            max_sort = db.execute(
                "SELECT COALESCE(MAX(sort_order), 0) m FROM kb_columns").fetchone()["m"]
            cur = db.execute(
                "INSERT INTO kb_columns (name, sort_order, kind, locked) "
                "VALUES (?, ?, 'kanban', 0)",
                ("Задачи", max_sort + 1))
            emi_target = cur.lastrowid
            print("[TeamBook] Откат Эйзенхауэра: создан обычный столбец-приёмник id=%s"
                  % emi_target)
        for ec in emi_cols:
            db.execute("UPDATE kb_tasks SET column_id=? WHERE column_id=?",
                       (emi_target, ec["id"]))
            db.execute("DELETE FROM kb_columns WHERE id=?", (ec["id"],))
        print("[TeamBook] Откат Эйзенхауэра: удалён квадрант-столбец id=%s" % ec["id"])
    # и убираем ставшее ненужным поле emi из задач (если колонка есть)
    if "emi" in kb_cols:
        try:
            db.execute("ALTER TABLE kb_tasks DROP COLUMN emi")
            print("[TeamBook] Откат Эйзенхауэра: удалена колонка emi из kb_tasks")
        except Exception:
            pass  # DROP COLUMN может быть недоступен в старых SQLite — колонка останется, код её не использует

    _rebuild_constrained_tables(db)

    # Индексы создаём после всех ALTER TABLE/rebuild, чтобы старые backup-файлы сначала
    # получили недостающие колонки, используемые partial-индексами.
    db.executescript(INDEX_SCHEMA)
    db.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,name) VALUES(1,'baseline')"
    )
    ledger = [tuple(r) for r in db.execute(
        "SELECT version,name FROM schema_migrations ORDER BY version"
    ).fetchall()]
    if not ledger or ledger[0] != (1, "baseline"):
        raise RuntimeError("Некорректный журнал schema_migrations: %r" % (ledger,))
    _run_versioned_migrations(db, MIGRATIONS)


def _migration_versioned_runner(db):
    """Маркер перехода на последовательный migration runner; DDL не требуется."""


def _migration_drop_legacy_space_owner(db):
    """Удаляет подтверждённые остаточные колонки отменённой spaces-функции."""
    for table in ("employee_history", "meetings", "problems"):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if "space_owner" in columns:
            db.execute(f"ALTER TABLE {table} DROP COLUMN space_owner")


MIGRATIONS = (
    (2, "versioned-runner", _migration_versioned_runner),
    (3, "drop-legacy-space-owner", _migration_drop_legacy_space_owner),
)


def _run_versioned_migrations(db, migrations):
    known = {version: name for version, name in db.execute(
        "SELECT version,name FROM schema_migrations"
    ).fetchall()}
    expected = {1: "baseline"}
    expected.update({version: name for version, name, _ in MIGRATIONS})
    expected.update({version: name for version, name, _ in migrations})
    for version, name in known.items():
        if expected.get(version) != name:
            raise RuntimeError("Неизвестная миграция %s/%s" % (version, name))
    for version, name, migrate in migrations:
        if known.get(version) == name:
            continue
        db.execute("SAVEPOINT versioned_migration")
        try:
            migrate(db)
            db.execute(
                "INSERT INTO schema_migrations(version,name) VALUES(?,?)",
                (version, name),
            )
        except Exception:
            try:
                db.execute("ROLLBACK TO versioned_migration")
                db.execute("RELEASE versioned_migration")
            except sqlite3.OperationalError as exc:
                raise RuntimeError(
                    "Миграция нарушила транзакцию; executescript запрещён"
                ) from exc
            raise
        try:
            db.execute("RELEASE versioned_migration")
        except sqlite3.OperationalError as exc:
            db.execute("DELETE FROM schema_migrations WHERE version=?", (version,))
            db.commit()
            raise RuntimeError(
                "Миграция нарушила транзакцию; executescript запрещён"
            ) from exc
        known[version] = name


def _repair_dangling_fk(db, table, fk_col):
    """Если таблица ссылается на employees_old (битая ссылка от переименования),
    пересоздаём её с корректным FK на employees, сохраняя данные."""
    import re
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        return
    sql = row["sql"]
    # Нормализуем битые ссылки на корректную (с кавычками или без)
    fixed = sql.replace('employees_old', 'employees')
    if fixed == sql:
        return  # ссылок на employees_old нет — всё в порядке

    print(f"[TeamBook] Починка FK: {table} ссылалась на employees_old "
          f"(битая ссылка от переименования), пересоздаю с FK на employees")

    # Пересоздаём под временным именем (имя в DDL может быть и с кавычками, и без)
    new_name = f"{table}_new"
    # имя таблицы в CREATE: "CREATE TABLE [\"]meetings[\"]"
    fixed_new = re.sub(
        r'(CREATE TABLE\s+)"?' + re.escape(table) + r'"?',
        r'\g<1>"' + new_name + '"',
        fixed,
    )
    db.execute(f"DROP TABLE IF EXISTS {new_name}")
    db.executescript(fixed_new)
    # копируем данные
    cols = [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    col_str = ", ".join(f'"{c}"' for c in cols)
    db.execute(f"INSERT INTO {new_name} ({col_str}) SELECT {col_str} FROM {table}")
    # подменяем
    db.execute(f"DROP TABLE {table}")
    db.execute(f"ALTER TABLE {new_name} RENAME TO \"{table}\"")


def _affinity(decl):
    """SQLite type affinity из объявленного типа (для сравнения сигнатур колонок)."""
    t = (decl or "").strip().upper()
    if "INT" in t:
        return "INTEGER"
    if ("CHAR" in t) or ("CLOB" in t) or ("TEXT" in t):
        return "TEXT"
    if ("REAL" in t) or ("FLOA" in t) or ("DOUB" in t):
        return "REAL"
    if ("BLOB" in t) or t == "":
        return "BLOB"
    return "NUMERIC"


def _norm_default(v):
    """Нормализация dflt_value для сравнения (снять скобки/пробелы, в нижний регистр)."""
    if v is None:
        return None
    s = str(v).strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    return s.lower()


def _col_signature(con, t):
    """{колонка: (аффинити-тип, notnull, default)} + набор колонок PRIMARY KEY."""
    cols = {}
    pk = set()
    for r in con.execute("PRAGMA table_info(%s)" % t):
        cols[r[1]] = (_affinity(r[2]), int(r[3]) == 1, _norm_default(r[4]))
        if r[5] > 0:
            pk.add(r[1])
    return cols, frozenset(pk)


def _unique_sets(con, t):
    uniques = set()
    # list(...): вложенный pragma_index_info на том же соединении иначе обнуляет
    # результаты незавершённого курсора index_list
    for (_seq, name, unique, origin, _partial) in list(
            con.execute("PRAGMA index_list(%s)" % t)):
        if unique and origin == "u":  # явный UNIQUE (не автоиндекс PK)
            uniques.add(frozenset(
                r[0] for r in con.execute(
                    "SELECT name FROM pragma_index_info(?)", (name,))))
    return uniques


def _fk_groups(con, t):
    """ПОЛНЫЕ определения внешних ключей: (parent, on_delete, on_update, match,
    последовательность (from,to) в порядке seq). Не только (from,parent,to) —
    иначе БД с ON DELETE NO ACTION вместо CASCADE прошла бы и ломала каскад."""
    groups = {}
    for r in con.execute("PRAGMA foreign_key_list(%s)" % t):
        gid = r[0]
        if gid not in groups:
            groups[gid] = {"ptable": r[2], "on_delete": r[6], "on_update": r[5],
                           "match": r[7], "cols": []}
        groups[gid]["cols"].append((r[3], r[4]))
    out = set()
    for group in groups.values():
        out.add((group["ptable"], group["on_delete"], group["on_update"], group["match"],
                 tuple(group["cols"])))
    return out


def _rowid_pk(con, t):
    """True, если PRIMARY KEY таблицы — настоящий SQLite rowid-alias (автогенерация id).

    rowid-alias = ОДНА колонка с ТОЧНЫМ declared-типом INTEGER (без AUTOINCREMENT тоже
    генерирует id), без WITHOUT ROWID и без отдельного 'pk'-индекса. BIGINT/TEXT/INTEGER
    PRIMARY KEY DESC/составной PK НЕ являются rowid-alias (создают sqlite_autoindex с
    origin='pk') — тогда INSERT без id даёт id=NULL и ломает lastrowid в маршрутах."""
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
    sql = (row[0] or "") if row else ""
    import re
    if re.search(r"\bWITHOUT\s+ROWID\b", sql or "", re.I):
        return False
    info = list(con.execute("PRAGMA table_info(%s)" % t))
    pkcols = [r for r in info if r[5] > 0]
    if len(pkcols) != 1:
        return False
    if (pkcols[0][2] or "").strip().upper() != "INTEGER":
        return False
    for (_seq, _name, _unique, origin, _partial) in list(
            con.execute("PRAGMA index_list(%s)" % t)):
        if origin == "pk":  # отдельный индекс PK -> не rowid-alias (BIGINT/DESC/TEXT/адемп)
            return False
    return True


def _ref_schema():
    """Эталонная обязательная схема из SCHEMA DDL (in-memory sqlite): для каждой
    таблицы — сигнатуры колонок (аффинити-тип, NOT NULL, default), PRIMARY KEY,
    UNIQUE-ограничения и ПОЛНЫЕ определения внешних ключей (on_delete/on_update/
    match, группировка/порядок столбцов)."""
    rcon = sqlite3.connect(":memory:")
    try:
        rcon.executescript(SCHEMA)
        tables = {r[0] for r in rcon.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        ref = {}
        for t in tables:
            cols, pk = _col_signature(rcon, t)
            ref[t] = {"cols": cols, "pk": pk,
                      "rowid": _rowid_pk(rcon, t),
                      "uniques": _unique_sets(rcon, t),
                      "fks": _fk_groups(rcon, t)}
        return ref
    finally:
        rcon.close()


def _validate_db_schema(con):
    """Строгая проверка БД против ПОЛНОЙ актуальной схемы (эталон из SCHEMA):
    - наличие всех таблиц и всех обязательных колонок;
    - сигнатуры колонок: аффинити-тип, NOT NULL, default (вкл. INTEGER PK/rowid —
      TEXT PRIMARY KEY не генерирует id и ломает lastrowid);
    - PRIMARY KEY (набор), UNIQUE-ограничения;
    - полные определения FK (on_delete/on_update/match, порядок/группировка);
    - integrity_check и PRAGMA foreign_key_check.
    foreign_key_check проверяет только целостность ДАННЫХ FK и не доказывает наличие
    самих контрактов (settings без PK -> ON CONFLICT; problems без CASCADE ->
    каскадное удаление сотрудника падает). Легитимные добавочные колонки/
    ограничения legacy-БД допускаются."""
    errors = []
    try:
        required = _ref_schema()
    except Exception as e:
        return ["не удалось построить эталонную схему: %s" % e]
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            errors.append("integrity_check != ok")
    except Exception as e:
        errors.append("integrity_check: %s" % e)
    tables = set()
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        allowed_tables = set(required) | {"sqlite_sequence"}
        extra_tables = sorted(tables - allowed_tables)
        if extra_tables:
            errors.append("лишние таблицы: %s" % ", ".join(extra_tables))
        missing = sorted(set(required) - tables)
        if missing:
            errors.append("нет таблиц: %s" % ", ".join(missing))
    except Exception as e:
        errors.append("чтение таблиц: %s" % e)
    try:
        forbidden = con.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('trigger','view') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if forbidden:
            errors.append("запрещены triggers/views: %s" % ", ".join(
                "%s %s" % (r[0], r[1]) for r in forbidden))
    except Exception as e:
        errors.append("чтение triggers/views: %s" % e)
    for tbl, spec in required.items():
        try:
            ccols, cpk = _col_signature(con, tbl)
            for col, (aff, nn, dflt) in spec["cols"].items():
                if col not in ccols:
                    errors.append("%s: нет колонки %s" % (tbl, col))
                    continue
                caff, cnn, cdflt = ccols[col]
                if caff != aff:
                    errors.append("%s.%s: тип должен быть %s-аффинити (факт %s)"
                                  % (tbl, col, aff, caff))
                if cnn != nn:
                    errors.append("%s.%s: NOT NULL должен быть %d (факт %d)"
                                  % (tbl, col, 1 if nn else 0, 1 if cnn else 0))
                if cdflt != dflt:
                    errors.append("%s.%s: default должен быть %r (факт %r)"
                                  % (tbl, col, dflt, cdflt))
            if cpk != spec["pk"]:
                errors.append(
                    "%s: PRIMARY KEY должен быть (%s), фактически (%s)"
                    % (tbl, ", ".join(sorted(spec["pk"])), ", ".join(sorted(cpk))))
            _c_rp = _rowid_pk(con, tbl)
            if _c_rp != spec["rowid"]:
                if spec["rowid"]:
                    errors.append(
                        "%s: PRIMARY KEY не является rowid-alias INTEGER (id не "
                        "генерируется, ломает lastrowid)" % tbl)
                else:
                    errors.append(
                        "%s: PRIMARY KEY не должен быть rowid-alias (структура "
                        "ключей не соответствует эталону)" % tbl)
            miss_un = sorted("(" + ", ".join(sorted(u)) + ")"
                             for u in spec["uniques"] if u not in _unique_sets(con, tbl))
            if miss_un:
                errors.append("%s: нет UNIQUE-ограничения %s" % (tbl, "; ".join(miss_un)))
            c_grp = _fk_groups(con, tbl)
            miss_fk = sorted(
                "%s ON DELETE %s ON UPDATE %s MATCH %s (%s)" % (
                    g[0], g[1], g[2], g[3],
                    ", ".join("%s->%s" % (a, b) for a, b in g[4]))
                for g in spec["fks"] if g not in c_grp)
            if miss_fk:
                errors.append("%s: отсутствуют FK-контракты %s"
                              % (tbl, "; ".join(miss_fk)))
        except Exception as e:
            errors.append("%s: проверка структуры: %s" % (tbl, e))
    for table, contracts in REQUIRED_CHECKS.items():
        try:
            ddl_row = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            actual = _normalized_check_contracts(ddl_row[0] if ddl_row else "")
            bad_checks = [name for name, expr in contracts.items()
                          if actual.get(name) != [expr]]
            if bad_checks:
                errors.append("%s: неверные CHECK-контракты %s" %
                              (table, ", ".join(bad_checks)))
        except Exception as e:
            errors.append("%s: проверка CHECK: %s" % (table, e))
    try:
        bad = con.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            errors.append("битые FK: %s" % str(bad[:5]))
    except Exception as e:
        errors.append("foreign_key_check: %s" % e)
    return errors


def _migrate_employees(db):
    """Переносим старые TEXT-поля position/department в справочники."""
    dest = DB_PATH + ".bak"
    # Отключаем автопереписывание FK-ссылок при переименовании, иначе SQLite
    # изменит references employees -> employees_old в дочерних таблицах.
    try:
        db.execute("PRAGMA legacy_alter_table = ON")
    except Exception:
        pass
    # Уникальные значения для справочников
    p_rows = db.execute(
        "SELECT DISTINCT trim(position) v FROM employees "
        "WHERE position IS NOT NULL AND trim(position) != ''"
    ).fetchall()
    d_rows = db.execute(
        "SELECT DISTINCT trim(department) v FROM employees "
        "WHERE department IS NOT NULL AND trim(department) != ''"
    ).fetchall()

    db.executemany("INSERT OR IGNORE INTO positions(name) VALUES (?)",
                   [(r["v"],) for r in p_rows])
    db.executemany("INSERT OR IGNORE INTO departments(name) VALUES (?)",
                   [(r["v"],) for r in d_rows])

    db.execute("ALTER TABLE employees RENAME TO employees_old")
    db.executescript(SCHEMA)  # создаст employees в новом виде
    db.execute(
        """
        INSERT INTO employees (id, name, position_id, department_id, salary, active, created_at)
        SELECT e.id, e.name,
               (SELECT p.id FROM positions p WHERE p.name = trim(e.position)),
               (SELECT d.id FROM departments d WHERE d.name = trim(e.department)),
               e.salary, e.active, e.created_at
        FROM employees_old e
        """
    )
    db.execute("DROP TABLE employees_old")
    print(f"[TeamBook] Миграция employees: добавлено "
          f"должностей={len(p_rows)}, отделов={len(d_rows)} (резерв: {dest})")


# --------------------------------------------------------------------------- #
# Авторизация
# --------------------------------------------------------------------------- #
@app.before_request
def ensure_auth():
    if request.endpoint in ("login", "static", "healthz") or request.endpoint is None:
        return
    if not session.get("authed"):
        return redirect(url_for("login"))


# --- CSRF-защита всех изменяющих web-путей ---
CSRF_EXEMPT = {"login"}


def _csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_hex(16)
        session["_csrf"] = tok
    return tok


@app.context_processor
def _inject_csrf_global():
    return {"csrf_token": _csrf_token()}


@app.before_request
def csrf_protect():
    """Защита от CSRF: требует валидный _csrf (форма или X-CSRF-Token) на всех
    POST, а также отвергает запрос с чужого Origin. /login (эндпоинт входа)
    проверяется только по Origin — на этапе логина токена в сессии ещё нет."""
    if request.method != "POST":
        return
    if request.content_length and request.content_length > app.config["MAX_CONTENT_LENGTH"]:
        abort(413)
    endpoint = request.endpoint
    if endpoint in CSRF_EXEMPT or endpoint is None:
        return
    origin = request.headers.get("Origin")
    if origin:
        from urllib.parse import urlparse
        o = urlparse(origin)
        if o.scheme not in ("http", "https") or (o.netloc and o.netloc != request.host):
            abort(400)
    # Сравниваем в байтах через secrets.compare_digest: передача строки напрямую
    # с не-ASCII (например _csrf=кириллица) бросает TypeError и даёт 500. Любая
    # ошибка/несовпадение -> 400 (fail closed).
    try:
        token = (request.form.get("_csrf")
                 or request.headers.get("X-CSRF-Token") or "").encode("utf-8")
        expected = (session.get("_csrf") or "").encode("utf-8")
        ok = bool(token) and bool(expected) and secrets.compare_digest(token, expected)
    except Exception:
        ok = False
    if not ok:
        abort(400)


@app.after_request
def _inject_csrf_forms(resp):
    """Встраиваем скрытый _csrf в каждый <form method="post"> при выдаче HTML —
    единый способ покрыть все web-формы без ручной вставки в каждом шаблоне."""
    if resp.status_code != 200:
        return resp
    if not session.get("_csrf"):
        return resp
    if "text/html" not in (resp.content_type or ""):
        return resp
    try:
        html = resp.get_data(as_text=True)
    except Exception:
        return resp
    if "<form" not in html:
        return resp
    import re as _re
    pat = _re.compile(r'(<form\b[^>]*\bmethod=["\']post["\'][^>]*>)', _re.I)
    inj = '<input type="hidden" name="_csrf" value="%s">' % session["_csrf"]
    out = pat.sub(lambda m: m.group(1) + inj, html)
    if out != html:
        resp.set_data(out.encode("utf-8"))
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        password = request.form.get("password", "")
        # fail-closed: без настроенного HR_PASSWORD вход невозможен
        if not ADMIN_PASSWORD:
            flash("Пароль не настроен — задайте переменную HR_PASSWORD", "error")
        elif not secrets.compare_digest(password.encode("utf-8"),
                                        ADMIN_PASSWORD.encode("utf-8")):
            if _login_rate_limit(ip, register_failure=True):
                return render_template("login.html"), 429
            flash("Неверный пароль", "error")
        else:
            if _login_rate_limit(ip, success=True):
                return render_template("login.html"), 429
            session.clear()
            session["authed"] = True
            session.permanent = True
            return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    if not session.get("authed"):
        return redirect(url_for("login"))
    session.clear()
    return redirect(url_for("login"))


def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped


# --------------------------------------------------------------------------- #
# Уведомления (шапка: выпадающее окно со счётчиком)
# --------------------------------------------------------------------------- #
def _notifications(db):
    """Активные задачи со сроком до сегодня+3 дней — кандидаты на уведомления.

    Возвращает dict: {overdue: [...], soon: [...], total: int}.
    overdue — срок уже вышел; soon — срок сегодня/завтра/+3 дня.
    """
    today = date.today()
    today_s = today.isoformat()
    horizon = (today + timedelta(days=3)).isoformat()
    rows = db.execute(
        """SELECT t.id, t.title, t.due_date, t.archived_at, t.deleted_at, c.name AS col_name
           FROM kb_tasks t
           LEFT JOIN kb_columns c ON c.id = t.column_id
           WHERE t.archived_at = '' AND t.deleted_at = ''
             AND t.due_date != '' AND t.due_date <= ?
           ORDER BY t.due_date ASC""",
        (horizon,),
    ).fetchall()
    members = _task_members_map(db)
    # личный todo тоже со сроками: цели в колокольчик (c 2026)
    todo_rows = db.execute(
        """SELECT id, title, due_date, '' AS col_name, '' AS emp
           FROM todo_items
           WHERE status != 'done' AND due_date != '' AND due_date <= ?
           ORDER BY due_date ASC""",
        (horizon,),
    ).fetchall()
    overdue, soon = [], []
    for r in rows:
        item = {
            "id": r["id"], "title": r["title"],
            "due_date": r["due_date"], "col_name": r["col_name"] or "—",
            "emp": ", ".join(m["name"] for m in members.get(r["id"], [])),
            "kind": "kanban",
        }
        if r["due_date"] < today_s:
            overdue.append(item)
        else:
            soon.append(item)
    for r in todo_rows:
        item = {
            "id": r["id"], "title": r["title"],
            "due_date": r["due_date"], "col_name": "📋 Мои задачи",
            "emp": "", "kind": "todo",
        }
        if r["due_date"] < today_s:
            overdue.append(item)
        else:
            soon.append(item)
    overdue = overdue[:15]
    soon = soon[:10]
    return {"overdue": overdue, "soon": soon,
            "total": len(overdue) + len(soon)}


@app.context_processor
def _inject_notifications():
    """Доступно в любом шаблоне под авторизованным пользователем."""
    if not session.get("authed"):
        return {"notifications": None}
    try:
        db = get_db()
        try:
            n = db.execute("SELECT COUNT(*) AS c FROM problems").fetchone()["c"]
        except Exception:
            n = 0
        notif = _notifications(db)
        # бейдж «Доска» в меню считает ТОЛЬКО просроченные задачи КАНБАНА
        # (не todo и без капа в 15, в отличие от выпадающего списка в колокольчике)
        try:
            board_overdue = db.execute(
                "SELECT COUNT(*) c FROM kb_tasks "
                "WHERE archived_at = '' AND deleted_at = '' "
                "AND due_date != '' AND due_date < ?",
                (date.today().isoformat(),)).fetchone()["c"]
        except Exception:
            board_overdue = 0
        # счётчик задач в личном todo (невыполненные: бэклог + квадранты)
        try:
            todo_cnt = db.execute(
                "SELECT COUNT(*) AS c FROM todo_items WHERE status != 'done'"
            ).fetchone()["c"]
        except Exception:
            todo_cnt = 0
        return {"notifications": notif, "problems_count": n,
                "board_overdue": board_overdue, "todo_cnt": todo_cnt}
    except Exception:
        return {"notifications": None}


_NAV_SECTION = {
    "index": "staff",
    "employee_view": "staff",
    "employee_form": "staff",
    "board": "board",
    "board_archive": "board",
    "board_trash": "board",
    "todo_page": "todo",
    "todo_archive": "todo",
    "problems_page": "problems",
    "catalogs": "refs",
    "settings_page": "settings",
    "backup_page": "settings",
}


@app.context_processor
def _inject_nav_section():
    """Какой раздел верхней навигации (и меню «Ещё») считать активным."""
    return {"nav_section": _NAV_SECTION.get(request.endpoint or "", "")}


# --------------------------------------------------------------------------- #
# Дашборд / разбивка по годам
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    db = get_db()

    # Фильтры по отделу и должности + поиск по имени/фамилии
    department = request.args.get("department", "").strip()
    position = request.args.get("position", "").strip()
    q = request.args.get("q", "").strip()

    all_departments = [r["name"] for r in db.execute(
        "SELECT DISTINCT d.name FROM departments d "
        "JOIN employees e ON e.department_id = d.id WHERE e.active = 1 "
        "ORDER BY d.name").fetchall()]
    all_positions = [r["name"] for r in db.execute(
        "SELECT DISTINCT p.name FROM positions p "
        "JOIN employees e ON e.position_id = p.id WHERE e.active = 1 "
        "ORDER BY p.name").fetchall()]

    where = ["e.active = 1"]
    params = []
    if department:
        where.append("d.name = ?")
        params.append(department)
    if position:
        where.append("p.name = ?")
        params.append(position)

    rows = db.execute(
        f"""
        SELECT e.*, p.name AS position, d.name AS department
        FROM employees e
        LEFT JOIN positions p ON p.id = e.position_id
        LEFT JOIN departments d ON d.id = e.department_id
        WHERE {' AND '.join(where)}
        ORDER BY d.name, e.name
        """,
        tuple(params),
    ).fetchall()

    employees = [
        {"id": r["id"], "name": r["name"], "position": r["position"],
         "department": r["department"], "salary": r["salary"], "tags": []}
        for r in rows
    ]

    # собрать теги всех показанных сотрудников одним запросом
    if employees:
        ids = [e["id"] for e in employees]
        marks = ",".join("?" * len(ids))
        tag_rows = db.execute(
            f"""SELECT et.employee_id, t.id, t.name, t.color FROM employee_tags et
                JOIN tags t ON t.id = et.tag_id
                WHERE et.employee_id IN ({marks}) ORDER BY t.name""",
            tuple(ids),
        ).fetchall()
        tagmap = {}
        for tr in tag_rows:
            tagmap.setdefault(tr["employee_id"], []).append(
                {"id": tr["id"], "name": tr["name"], "color": tr["color"]})
        for e in employees:
            e["tags"] = tagmap.get(e["id"], [])

    # Поиск по имени/фамилии (регистронезависимо, поддержка кириллицы)
    if q:
        ql = q.lower().replace("ё", "е")
        employees = [e for e in employees
                     if ql in e["name"].lower().replace("ё", "е")]

    # --- сводка «цифры недели» (главная, над таблицей сотрудников) ---
    today_iso = date.today().isoformat()
    open_tasks = db.execute(
        "SELECT COUNT(*) c FROM kb_tasks WHERE archived_at = '' AND deleted_at = ''").fetchone()["c"]
    overdue_tasks = db.execute(
        "SELECT COUNT(*) c FROM kb_tasks "
        "WHERE archived_at = '' AND deleted_at = '' AND due_date != '' AND due_date < ?",
        (today_iso,)).fetchone()["c"]
    problems_count = db.execute("SELECT COUNT(*) c FROM problems").fetchone()["c"]
    this_year = datetime.now().year
    records_count = db.execute(
        "SELECT COUNT(*) c FROM year_records WHERE year = ?",
        (this_year,)).fetchone()["c"]
    todo_open = db.execute(
        "SELECT COUNT(*) c FROM todo_items WHERE status != 'done'").fetchone()["c"]
    todo_done_today = db.execute(
        "SELECT COUNT(*) c FROM todo_items WHERE status='done' AND done_date=?",
        (today_iso,)).fetchone()["c"]
    stats = {
        "open_tasks": open_tasks,
        "overdue_tasks": overdue_tasks,
        "problems": problems_count,
        "records": records_count,
        "year": this_year,
        "todo_open": todo_open,
        "todo_done_today": todo_done_today,
    }

    return render_template(
        "index.html",
        employees=employees,
        departments=all_departments,
        positions=all_positions,
        sel_department=department,
        sel_position=position,
        sel_q=q,
        now_year=datetime.now().year,
        stats=stats,
    )


# --------------------------------------------------------------------------- #
# Справочники: должности и отделы
# --------------------------------------------------------------------------- #
@app.route("/catalogs")
@login_required
def catalogs():
    db = get_db()
    positions = db.execute("SELECT * FROM positions ORDER BY name").fetchall()
    departments = db.execute("SELECT * FROM departments ORDER BY name").fetchall()
    tags = db.execute("SELECT id, name, color, "
                      "(SELECT COUNT(*) FROM employee_tags et WHERE et.tag_id=tags.id) AS used "
                      "FROM tags ORDER BY name").fetchall()
    return render_template("catalogs.html", positions=positions,
                           departments=departments, tags=tags)


# Палитра цветов для тегов (по умолчанию при создании)
TAG_COLORS = ["#5B8DEF", "#E4572E", "#1B998B", "#E9C46A", "#C44536",
              "#772D8B", "#2A9D8F", "#D9667A", "#3F8B4F", "#6D597A", "#17A2B8", "#E07A5F"]


@app.route("/catalog/<kind>/add", methods=["POST"])
@login_required
def catalog_add(kind):
    if kind not in ("position", "department", "tag"):
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Название не может быть пустым", "error")
    else:
        db = get_db()
        try:
            if kind == "tag":
                n = db.execute("SELECT COUNT(*) c FROM tags").fetchone()["c"]
                color = TAG_COLORS[n % len(TAG_COLORS)]
                db.execute("INSERT INTO tags (name, color) VALUES (?,?)", (name, color))
            else:
                db.execute(f"INSERT INTO {kind}s (name) VALUES (?)", (name,))
            db.commit()
            flash(f"Добавлено: {name}", "ok")
        except sqlite3.IntegrityError:
            flash(f"«{name}» уже существует", "error")
    return redirect(url_for("catalogs"))


@app.route("/catalog/<kind>/<int:cid>/rename", methods=["POST"])
@login_required
def catalog_rename(kind, cid):
    if kind not in ("position", "department", "tag"):
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Название не может быть пустым", "error")
    else:
        db = get_db()
        try:
            db.execute(f"UPDATE {kind}s SET name=? WHERE id=?", (name, cid))
            db.commit()
            flash("Переименовано", "ok")
        except sqlite3.IntegrityError:
            flash(f"«{name}» уже существует", "error")
    return redirect(url_for("catalogs"))


@app.route("/catalog/<kind>/<int:cid>/delete", methods=["POST"])
@login_required
def catalog_delete(kind, cid):
    if kind not in ("position", "department", "tag"):
        abort(404)
    db = get_db()
    # SET NULL снимет ссылку с сотрудников; для тегов — снимем связи в employee_tags
    if kind == "tag":
        db.execute("DELETE FROM employee_tags WHERE tag_id=?", (cid,))
    db.execute(f"DELETE FROM {kind}s WHERE id=?", (cid,))
    db.commit()
    flash("Удалено", "ok")
    return redirect(url_for("catalogs"))


# --------------------------------------------------------------------------- #
# Сотрудники CRUD
# --------------------------------------------------------------------------- #
def _cat_options(db):
    positions = db.execute("SELECT * FROM positions ORDER BY name").fetchall()
    departments = db.execute("SELECT * FROM departments ORDER BY name").fetchall()
    return positions, departments


@app.route("/employee/new", methods=["GET", "POST"])
@login_required
def employee_new():
    db = get_db()
    positions, departments = _cat_options(db)
    if request.method == "POST":
        return _save_employee(None)
    tags = db.execute("SELECT * FROM tags ORDER BY name").fetchall()
    return render_template("employee_form.html", emp={}, title="Новый сотрудник",
                           positions=positions, departments=departments, tags=tags,
                           employee_tag_ids=[])


@app.route("/employee/<int:eid>/edit", methods=["GET", "POST"])
@login_required
def employee_edit(eid):
    db = get_db()
    emp = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not emp:
        abort(404)
    if request.method == "POST":
        return _save_employee(eid)
    positions, departments = _cat_options(db)
    tags = db.execute("SELECT * FROM tags ORDER BY name").fetchall()
    return render_template("employee_form.html", emp=emp, title="Редактирование",
                           positions=positions, departments=departments, tags=tags,
                           employee_tag_ids=_employee_tags(db, eid))


def _clean_int(val):
    """'' -> None; остальное -> int или None."""
    try:
        v = int(val)
        return v
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Настройки (key-value) — Telegram-бот и пр.
# --------------------------------------------------------------------------- #
def get_setting(db, key, default=""):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r and r["value"] is not None else default


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, "" if value is None else str(value)))


def _save_employee(eid):
    db = get_db()
    name = request.form.get("name", "").strip()
    if not name:
        flash("Имя обязательно", "error")
        return redirect(url_for("employee_new") if eid is None
                        else url_for("employee_edit", eid=eid))
    pid = _clean_int(request.form.get("position_id"))
    did = _clean_int(request.form.get("department_id"))
    salary = request.form.get("salary", "").strip()
    hire_date = request.form.get("hire_date", "").strip()
    if not _valid_iso_date(hire_date):
        abort(400, "Некорректная дата приёма")

    if eid is None:
        cur = db.execute(
            "INSERT INTO employees (name, position_id, department_id, salary, hire_date) "
            "VALUES (?,?,?,?,?)",
            (name, pid, did, salary, hire_date),
        )
        eid = cur.lastrowid
    else:
        db.execute(
            "UPDATE employees SET name=?, position_id=?, department_id=?, salary=?, hire_date=? "
            "WHERE id=?",
            (name, pid, did, salary, hire_date, eid),
        )
    # Теги сотрудника (many-to-many) — перезапись связей
    _save_employee_tags(db, eid, request.form.getlist("tag_ids"))
    db.commit()
    flash("Сотрудник сохранён", "ok")
    return redirect(url_for("employee_view", eid=eid))


def _save_employee_tags(db, eid, tag_ids):
    """Обновить набор тегов сотрудника (перезапись many-to-many)."""
    db.execute("DELETE FROM employee_tags WHERE employee_id=?", (eid,))
    seen = set()
    for v in tag_ids:
        t = _clean_int(v)
        if t is not None and t not in seen:
            seen.add(t)
            db.execute("INSERT OR IGNORE INTO employee_tags (employee_id, tag_id) "
                       "VALUES (?,?)", (eid, t))


def _employee_tags(db, eid):
    """Вернуть список тегов сотрудника."""
    return [r["tag_id"] for r in db.execute(
        "SELECT tag_id FROM employee_tags WHERE employee_id=?", (eid,)).fetchall()]


@app.route("/employee/<int:eid>")
@login_required
def employee_view(eid):
    db = get_db()
    emp = db.execute(
        """
        SELECT e.*, p.name AS position, d.name AS department
        FROM employees e
        LEFT JOIN positions p ON p.id = e.position_id
        LEFT JOIN departments d ON d.id = e.department_id
        WHERE e.id=?
        """,
        (eid,),
    ).fetchone()
    if not emp:
        abort(404)
    years = db.execute(
        "SELECT DISTINCT year FROM year_records WHERE employee_id=? ORDER BY year DESC",
        (eid,),
    ).fetchall()
    year = request.args.get("year", type=int, default=None)
    if year is None and years:
        year = years[0]["year"]

    # Теги сотрудника
    emp_tags = db.execute(
        """SELECT t.id, t.name, t.color FROM tags t
           JOIN employee_tags et ON et.tag_id = t.id
           WHERE et.employee_id=? ORDER BY t.name""", (eid,)
    ).fetchall()

    records = db.execute(
        "SELECT * FROM year_records WHERE employee_id=? AND year=?",
        (eid, year),
    ).fetchall()
    records = {r["semester"]: r for r in records}

    meetings = db.execute(
        "SELECT * FROM meetings WHERE employee_id=? ORDER BY date DESC, id DESC",
        (eid,),
    ).fetchall()

    problems = db.execute(
        "SELECT * FROM problems WHERE employee_id=? ORDER BY id DESC",
        (eid,),
    ).fetchall()

    # История изменений должности/зарплаты (датированная)
    history = db.execute(
        """
        SELECT h.*, p.name AS position_name
        FROM employee_history h
        LEFT JOIN positions p ON p.id = h.position_id
        WHERE h.employee_id=?
        ORDER BY h.change_date DESC, h.id DESC
        """,
        (eid,),
    ).fetchall()
    positions, _ = _cat_options(db)

    return render_template(
        "employee_view.html",
        emp=emp,
        years=[y["year"] for y in years],
        year=year,
        records=records,
        semesters=SEMESTERS,
        meetings=meetings,
        history=history,
        positions=positions,
        emp_tags=emp_tags,
        problems=problems,
        now_year=datetime.now().year,
    )


@app.route("/employee/<int:eid>/delete", methods=["POST"])
@login_required
def employee_delete(eid):
    db = get_db()
    db.execute("DELETE FROM employees WHERE id=?", (eid,))
    db.commit()
    flash("Сотрудник удалён", "ok")
    return redirect(url_for("index"))


@app.route("/employee/<int:eid>/notes", methods=["POST"])
@login_required
def employee_notes(eid):
    db = get_db()
    notes = request.form.get("notes", "")
    db.execute("UPDATE employees SET notes=? WHERE id=?", (notes, eid))
    db.commit()
    flash("Заметки сохранены", "ok")
    return redirect(url_for("employee_view", eid=eid))


@app.route("/employee/<int:eid>/history/add", methods=["POST"])
@login_required
def history_add(eid):
    db = get_db()
    change_date = request.form.get("change_date", "").strip()
    if not change_date:
        flash("Дата изменения обязательна", "error")
        return redirect(url_for("employee_view", eid=eid))
    if not _valid_iso_date(change_date):
        abort(400, "Некорректная дата изменения")
    db.execute(
        "INSERT INTO employee_history (employee_id, change_date, position_id, salary, note) "
        "VALUES (?,?,?,?,?)",
        (eid, change_date, _clean_int(request.form.get("position_id")),
         request.form.get("salary", "").strip(), request.form.get("note", "").strip()),
    )
    db.commit()
    flash("Запись истории добавлена", "ok")
    return redirect(url_for("employee_view", eid=eid))


@app.route("/history/<int:hid>/edit", methods=["POST"])
@login_required
def history_edit(hid):
    db = get_db()
    rec = db.execute("SELECT * FROM employee_history WHERE id=?", (hid,)).fetchone()
    if not rec:
        abort(404)
    change_date = request.form.get("change_date", rec["change_date"]).strip()
    if not _valid_iso_date(change_date):
        abort(400, "Некорректная дата изменения")
    db.execute(
        "UPDATE employee_history SET change_date=?, position_id=?, salary=?, note=? WHERE id=?",
        (change_date, _clean_int(request.form.get("position_id")),
         request.form.get("salary", "").strip(), request.form.get("note", "").strip(), hid),
    )
    db.commit()
    flash("Запись истории обновлена", "ok")
    return redirect(url_for("employee_view", eid=rec["employee_id"]))


@app.route("/history/<int:hid>/delete", methods=["POST"])
@login_required
def history_delete(hid):
    db = get_db()
    rec = db.execute("SELECT * FROM employee_history WHERE id=?", (hid,)).fetchone()
    if rec:
        db.execute("DELETE FROM employee_history WHERE id=?", (hid,))
        db.commit()
        flash("Запись истории удалена", "ok")
        return redirect(url_for("employee_view", eid=rec["employee_id"]))
    abort(404)


# --------------------------------------------------------------------------- #
# Полугодовые записи
# --------------------------------------------------------------------------- #
@app.route("/employee/<int:eid>/record", methods=["POST"])
@login_required
def record_save(eid):
    db = get_db()
    year = request.form.get("year", "0").strip()
    semester = request.form.get("semester", "").strip()
    try:
        year = int(year)
    except ValueError:
        abort(400)
    if semester not in ("1H", "2H"):
        abort(400, "Некорректное полугодие")
    db.execute(
        """
        INSERT INTO year_records (employee_id, year, semester, goals_employee,
            proposals_manager, wishes_employee, comments, colleagues_feedback, updated_at)
        VALUES (?,?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(employee_id, year, semester) DO UPDATE SET
            goals_employee=excluded.goals_employee,
            proposals_manager=excluded.proposals_manager,
            wishes_employee=excluded.wishes_employee,
            comments=excluded.comments,
            colleagues_feedback=excluded.colleagues_feedback,
            updated_at=datetime('now')
        """,
        (
            eid, year, semester,
            request.form.get("goals_employee", ""),
            request.form.get("proposals_manager", ""),
            request.form.get("wishes_employee", ""),
            request.form.get("comments", ""),
            request.form.get("colleagues_feedback", ""),
        ),
    )
    db.commit()
    flash("Запись сохранена", "ok")
    return redirect(url_for("employee_view", eid=eid, year=year))


@app.route("/record/<int:rid>/delete", methods=["POST"])
@login_required
def record_delete(rid):
    db = get_db()
    rec = db.execute("SELECT * FROM year_records WHERE id=?", (rid,)).fetchone()
    if rec:
        eid, year = rec["employee_id"], rec["year"]
        db.execute("DELETE FROM year_records WHERE id=?", (rid,))
        db.commit()
        return redirect(url_for("employee_view", eid=eid, year=year))
    abort(404)


@app.route("/employee/<int:eid>/year/new", methods=["POST"])
@login_required
def employee_year_new(eid):
    """Создать каркас года для сотрудника: две пустые полугодовые записи (1H, 2H)."""
    db = get_db()
    try:
        year = int(request.form.get("year", "").strip())
    except ValueError:
        flash("Укажите корректный год", "error")
        return redirect(url_for("employee_view", eid=eid))
    for sem in ("1H", "2H"):
        db.execute(
            "INSERT OR IGNORE INTO year_records "
            "(employee_id, year, semester, updated_at) VALUES (?,?,?, datetime('now'))",
            (eid, year, sem),
        )
    db.commit()
    flash(f"Год {year} создан для сотрудника", "ok")
    return redirect(url_for("employee_view", eid=eid, year=year))


@app.route("/employee/<int:eid>/year/delete", methods=["POST"])
@login_required
def employee_year_delete(eid):
    """Удалить год целиком: все полугодовые записи (1H, 2H) сотрудника за год."""
    db = get_db()
    try:
        year = int(request.form.get("year", "").strip())
    except ValueError:
        flash("Укажите корректный год", "error")
        return redirect(url_for("employee_view", eid=eid))
    deleted = db.execute(
        "DELETE FROM year_records WHERE employee_id=? AND year=?",
        (eid, year),
    ).rowcount
    db.commit()
    if deleted:
        flash(f"Год {year} и его записи удалены", "ok")
    else:
        flash(f"Года {year} у сотрудника не было", "error")
    return redirect(url_for("employee_view", eid=eid))


# --------------------------------------------------------------------------- #
# Встречи 1-на-1
# --------------------------------------------------------------------------- #
@app.route("/employee/<int:eid>/meeting/new", methods=["GET", "POST"])
@login_required
def meeting_new(eid):
    db = get_db()
    if request.method == "POST":
        date = request.form.get("date", "") or datetime.now().strftime("%Y-%m-%d")
        if not _valid_iso_date(date):
            abort(400, "Некорректная дата встречи")
        db.execute(
            "INSERT INTO meetings (employee_id, date, summary) VALUES (?,?,?)",
            (eid, date, request.form.get("summary", "")),
        )
        db.commit()
        flash("Встреча добавлена", "ok")
        return redirect(url_for("employee_view", eid=eid))
    return render_template("meeting_form.html", eid=eid, mt={}, title="Новая встреча")


@app.route("/meeting/<int:mid>/edit", methods=["GET", "POST"])
@login_required
def meeting_edit(mid):
    db = get_db()
    mt = db.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
    if not mt:
        abort(404)
    if request.method == "POST":
        date = request.form.get("date", mt["date"])
        if not _valid_iso_date(date):
            abort(400, "Некорректная дата встречи")
        db.execute(
            "UPDATE meetings SET date=?, summary=? WHERE id=?",
            (date, request.form.get("summary", ""), mid),
        )
        db.commit()
        flash("Встреча обновлена", "ok")
        return redirect(url_for("employee_view", eid=mt["employee_id"]))
    return render_template("meeting_form.html", eid=mt["employee_id"], mt=mt,
                           title="Редактирование встречи")


@app.route("/meeting/<int:mid>/delete", methods=["POST"])
@login_required
def meeting_delete(mid):
    db = get_db()
    mt = db.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
    if mt:
        db.execute("DELETE FROM meetings WHERE id=?", (mid,))
        db.commit()
        return redirect(url_for("employee_view", eid=mt["employee_id"]))
    abort(404)


# --------------------------------------------------------------------------- #
# Проблемы сотрудников
# --------------------------------------------------------------------------- #
@app.route("/problems")
@login_required
def problems_page():
    """Общий список: сотрудники, у которых есть зафиксированные проблемы."""
    db = get_db()
    rows = db.execute(
        """SELECT e.id AS eid, e.name AS name, p.id AS pid, p.text AS text, p.created_at
           FROM problems p
           JOIN employees e ON e.id = p.employee_id
           ORDER BY e.name COLLATE NOCASE, p.id DESC""",
    ).fetchall()
    # сгруппировать по сотруднику
    by_emp = {}
    for r in rows:
        by_emp.setdefault(r["eid"], {"name": r["name"], "problems": []})["problems"].append(r)
    employees = db.execute("SELECT id, name FROM employees ORDER BY name COLLATE NOCASE").fetchall()
    return render_template("problems.html", by_emp=by_emp, employees=employees)


@app.route("/employee/<int:eid>/problem/add", methods=["POST"])
@login_required
def problem_add(eid):
    text = request.form.get("text", "").strip()
    if text:
        db = get_db()
        db.execute("INSERT INTO problems (employee_id, text) VALUES (?,?)", (eid, text))
        db.commit()
        flash("Проблема добавлена", "ok")
    return redirect(url_for("employee_view", eid=eid))


@app.route("/problem/add", methods=["POST"])
@login_required
def problem_add_general():
    """Добавить проблему из общего списка (выбор сотрудника в форме)."""
    eid = _clean_int(request.form.get("employee_id"))
    text = request.form.get("text", "").strip()
    if eid and text:
        db = get_db()
        db.execute("INSERT INTO problems (employee_id, text) VALUES (?,?)", (eid, text))
        db.commit()
        flash("Проблема добавлена", "ok")
    return redirect(url_for("problems_page"))


@app.route("/problem/<int:pid>/delete", methods=["POST"])
@login_required
def problem_delete(pid):
    db = get_db()
    p = db.execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone()
    if p:
        db.execute("DELETE FROM problems WHERE id=?", (pid,))
        db.commit()
        flash("Проблема удалена", "ok")
        return redirect(url_for("employee_view", eid=p["employee_id"]))
    abort(404)


# --------------------------------------------------------------------------- #
# Резервное копирование / восстановление БД
# --------------------------------------------------------------------------- #
@app.route("/healthz")
def healthz():
    """Unauthenticated liveness response with non-secret build identity."""
    return {"status": "ok", "commit": COMMIT_SHA or "unknown"}


@app.route("/backup")
@login_required
def backup_page():
    from os import path as _p
    size = _p.getsize(DB_PATH) if _p.exists(DB_PATH) else 0
    return render_template("backup.html", db_size=size,
                           db_modified=_p.getmtime(DB_PATH) if _p.exists(DB_PATH) else 0)


@app.route("/backup/export")
@login_required
def backup_export():
    """Скачать копию БД: делаем консистентную копию через SQLite backup API."""
    from os import path as _p
    from io import BytesIO
    _p.exists(DB_PATH) or abort(404)
    # Создаём консистентную копию во временном файле, читаем её в память и сразу
    # удаляем файл — никаких «висячих» temp-файлов/дескрипторов после ответа
    # (и это надёжно работает на любой ОС, включая Windows с блокировкой файлов).
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)  # закрываем дескриптор от mkstemp, иначе на Windows удаление заблокировано
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        src.close(); dst.close()
        with open(tmp, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"teambook_backup_{stamp}.db"
    return send_file(BytesIO(data), as_attachment=True, download_name=fname,
                     mimetype="application/vnd.sqlite3", max_age=0)


@app.route("/backup/import", methods=["POST"])
@login_required
def backup_import():
    """Восстановить БД из загруженного файла. Текущая БД бэкапируется рядом."""
    if request.content_length and request.content_length > app.config["MAX_CONTENT_LENGTH"]:
        abort(413)
    f = request.files.get("dbfile")
    if not f or not f.filename:
        flash("Не выбран файл для восстановления", "error")
        return redirect(url_for("backup_page"))

    # Валидация: должен быть корректный SQLite-файл с нашими таблицами
    suffix = "" if f.filename.lower().endswith(".db") else ".db"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        f.save(tf.name)
        upload_path = tf.name

    ok = False
    con = None
    try:
        con = sqlite3.connect(upload_path)
        con.row_factory = sqlite3.Row
        # проверяем "сигнатуру" SQLite и наличие ключевых таблиц
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "employees" not in tables or "positions" not in tables or "departments" not in tables:
            flash("Файл не похож на БД TeamBook (не найдены таблицы)", "error")
            ok = False
        elif con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            flash("Файл повреждён: integrity_check не прошёл", "error")
            ok = False
        else:
            ok = True
    except Exception as e:
        flash(f"Не удалось прочитать файл: {e}", "error")
    finally:
        if con is not None:
            con.close()
        if not ok and os.path.exists(upload_path):
            os.remove(upload_path)

    if not ok:
        return redirect(url_for("backup_page"))

    # Мигрируем загруженную копию ДО активации: в старой БД могут отсутствовать
    # новые колонки/таблицы (например tag в todo_items) — без этого /todo упадёт
    # с «no such column». При ошибке исходная БД не трогается.
    migrate_ok = True
    try:
        mcon = sqlite3.connect(upload_path)
        mcon.row_factory = sqlite3.Row  # _apply_migrations/_repair_dangling_fk требуют named-access
        try:
            _apply_migrations(mcon)
            mcon.commit()
            # строгая проверка после миграции: одной успешной миграции мало,
            # схема и внешние ключи должны быть целыми до активации
            errs = _validate_db_schema(mcon)
            if errs:
                raise ValueError("схема после миграции невалидна: %s" % "; ".join(errs))
        finally:
            mcon.close()
    except Exception as e:
        migrate_ok = False
        flash(f"Не удалось привести файл к актуальной схеме: {e}", "error")

    if not migrate_ok:
        if os.path.exists(upload_path):
            os.remove(upload_path)
        return redirect(url_for("backup_page"))

    # Онлайн-замена SQLite/WAL небезопасна: активные workers держат старый inode.
    # Передаём проверенную БД PID1; entrypoint остановит Gunicorn и bot, заменит БД
    # при отсутствии открытых соединений, затем перезапустит весь контейнер.
    import json
    staged = os.path.join(DATA_DIR, ".restore-staged-%s.db" % secrets.token_hex(8))
    try:
        if os.path.exists(RESTORE_REQUEST):
            raise RuntimeError("восстановление уже ожидает активации")
        os.replace(upload_path, staged)
        os.chmod(staged, 0o600)
        request_tmp = RESTORE_REQUEST + ".tmp-" + secrets.token_hex(4)
        fd = os.open(request_tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"staged": staged, "requested_at": datetime.now().isoformat()}, fh)
            fh.flush(); os.fsync(fh.fileno())
        # Не перезаписываем уже ожидающий request.
        os.link(request_tmp, RESTORE_REQUEST)
        os.remove(request_tmp)
    except Exception as e:
        for path in (upload_path, staged):
            try: os.remove(path)
            except FileNotFoundError: pass
        flash("Не удалось поставить восстановление в очередь: %s" % e, "error")
        return redirect(url_for("backup_page"))

    flash("База проверена. Восстановление началось; приложение перезапустится.", "ok")
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Настройки (резервная копия + Telegram-бот)
# --------------------------------------------------------------------------- #
@app.route("/settings")
@login_required
def settings_page():
    from os import path as _p
    db = get_db()
    size = _p.getsize(DB_PATH) if _p.exists(DB_PATH) else 0
    tg_token = get_setting(db, "tg_token")
    tg_admin = get_setting(db, "tg_admin_id")
    tg_enabled = get_setting(db, "tg_enabled", "0") == "1"
    return render_template(
        "settings.html", db_size=size,
        db_modified=_p.getmtime(DB_PATH) if _p.exists(DB_PATH) else 0,
        tg_token=tg_token, tg_admin=tg_admin, tg_enabled=tg_enabled)


@app.route("/settings/bot/save", methods=["POST"])
@login_required
def settings_bot_save():
    """Сохранить настройки бота (токен и админ) — сам запуск происходит
    ботом-процессом автоматически, когда заполнен токен и включён флаг."""
    db = get_db()
    token = request.form.get("tg_token", "").strip()
    admin = request.form.get("tg_admin", "").strip()
    enabled = 1 if request.form.get("tg_enabled") == "1" else 0
    set_setting(db, "tg_token", token)
    set_setting(db, "tg_admin_id", admin)
    set_setting(db, "tg_enabled", "1" if enabled else "0")
    db.commit()
    flash("Настройки Telegram-бота сохранены", "ok")
    return redirect(url_for("settings_page"))


# --------------------------------------------------------------------------- #
# Экспорт в Excel (отчёт по полугодовым записям)
# --------------------------------------------------------------------------- #
def _report_data(db, eid=None, year=None):
    """Собрать строки отчёта: по всем сотрудникам или по одному (eid)."""
    year = year or datetime.now().year
    if eid is None:
        rows = db.execute(
            """
            SELECT e.id, e.name, e.salary, e.hire_date,
                   p.name AS position, d.name AS department
            FROM employees e
            LEFT JOIN positions p ON p.id = e.position_id
            LEFT JOIN departments d ON d.id = e.department_id
            WHERE e.active = 1
            ORDER BY d.name, e.name
            """
        ).fetchall()
        emp_ids = [r["id"] for r in rows]
        by_id = {r["id"]: r for r in rows}
    else:
        e = db.execute(
            """
            SELECT e.id, e.name, e.salary, e.hire_date,
                   p.name AS position, d.name AS department
            FROM employees e
            LEFT JOIN positions p ON p.id = e.position_id
            LEFT JOIN departments d ON d.id = e.department_id
            WHERE e.id=?
            """, (eid,)
        ).fetchone()
        if not e:
            abort(404)
        emp_ids = [e["id"]]
        by_id = {e["id"]: e}

    recs = {}
    if emp_ids:
        marks = ",".join("?" * len(emp_ids))
        rr = db.execute(
            f"""
            SELECT employee_id, semester, goals_employee, proposals_manager,
                   wishes_employee, comments, colleagues_feedback, updated_at
            FROM year_records WHERE year=? AND employee_id IN ({marks})
            """, (year, *emp_ids)
        ).fetchall()
        for r in rr:
            recs.setdefault(r["employee_id"], {})[r["semester"]] = r

    # встречи (для отчёта по одному сотруднику)
    meetings = {}
    if eid:
        mm = db.execute(
            "SELECT date, summary FROM meetings WHERE employee_id=? "
            "ORDER BY date DESC, id DESC", (eid,)
        ).fetchall()
        meetings = [{"date": m["date"], "summary": m["summary"]} for m in mm]

    out = []
    for i in emp_ids:
        e = by_id[i]
        out.append({
            "name": e["name"],
            "department": e["department"] or "—",
            "position": e["position"] or "—",
            "salary": e["salary"] or "—",
            "hire_date": e["hire_date"] or "",
            "year": year,
            "semesters": {
                s: dict(recs.get(i, {}).get(s) or {}) for s in SEMESTERS
            },
            "meetings": meetings if eid else [],
        })
    return out


def _fmt_date(v):
    s = (v or "")
    return s[:10] if s else ""


def _valid_iso_date(value):
    """Пустая дата допустима; непустая должна строго соответствовать YYYY-MM-DD."""
    if not value:
        return True
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _safe_sheet_name(s, limit=31):
    r"""Excel-запрещённые символы []:*?/\ и апострофы + лимит длины листа."""
    import re
    s = re.sub(r"[\[\]:*/?\\]", "", str(s or "")).replace("'", "")
    return (s[:limit].strip() or "Отчёт")


def _xml_escape(s):
    """Экранирует пользовательский текст перед Paragraph (reportlab): иначе
    незакрытый '<b>' или '&' сломают рендер PDF."""
    from xml.sax.saxutils import escape
    return escape(str(s if s is not None else ""))


def _send_file_cleanup(src, name, mimetype):
    """send_file + удаление временного файла (src как путь) после ответа,
    чтобы отчёты не оставляли мусор в temp."""
    resp = send_file(src, as_attachment=True, download_name=name,
                     mimetype=mimetype, max_age=0)
    if isinstance(src, str):
        @resp.call_on_close
        def _rm_report_tmp():
            try:
                if os.path.exists(src):
                    os.remove(src)
            except Exception:
                pass
    return resp


def _build_report_rows(data):
    """Превратить данные отчёта в плоские строки для Excel."""
    base = ["Сотрудник", "Отдел", "Должность", "Зарплата", "Дата приёма"]
    sem_cols = ["Цели", "Мои предложения", "Пожелания", "Мои комментарии", "Отзывы коллег"]
    headers = list(base)
    for code in SEMESTERS:
        labels = {"1H": "I полугодие", "2H": "II полугодие"}
        for c in sem_cols:
            headers.append(f"{labels[code]}: {c}")

    rows = []
    for item in data:
        row = [
            _xlsx_safe(item["name"]), _xlsx_safe(item["department"]),
            _xlsx_safe(item["position"]), _xlsx_safe(item["salary"]),
            _xlsx_safe(_fmt_date(item["hire_date"])),
        ]
        for code in SEMESTERS:
            rec = item["semesters"].get(code) or {}
            for field in ["goals_employee", "proposals_manager",
                          "wishes_employee", "comments", "colleagues_feedback"]:
                row.append(_xlsx_safe((rec.get(field) or "").strip()))
        rows.append(row)
    return headers, rows


def _xlsx_safe(value):
    """Не даёт Excel выполнить пользовательскую строку как формулу."""
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if stripped.startswith(("=", "+", "-", "@")):
        leading = value[:len(value) - len(stripped)]
        return leading + "'" + stripped
    return value


def _register_pdf_font():
    """Зарегистрировать TTF-шрифт с кириллицей (DejaVu в образе, Arial на Windows)."""
    candidates = {
        "DejaVuSans": [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ],
        "DejaVuSans-Bold": [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ],
    }
    # Windows fallback (только для локальной разработки)
    win = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    candidates["DejaVuSans"] += [os.path.join(win, "arial.ttf"),
                                 os.path.join(win, "DejaVuSans.ttf")]
    candidates["DejaVuSans-Bold"] += [os.path.join(win, "arialbd.ttf"),
                                      os.path.join(win, "DejaVuSans-Bold.ttf")]
    for name, paths in candidates.items():
        for p in paths:
            if os.path.exists(p):
                try:
                    pdfmetrics.registerFont(TTFont(name, p))
                    break
                except Exception:
                    continue
    # aligned fallback
    if "DejaVuSans" not in pdfmetrics.getRegisteredFontNames() and \
       "Arial" in pdfmetrics.getRegisteredFontNames():
        return "Arial"
    if "DejaVuSans" in pdfmetrics.getRegisteredFontNames():
        return "DejaVuSans"
    return "Helvetica"


def _build_report_pdf(data, title):
    """Сгенерировать PDF-отчёт (таблица полугодий)."""
    fn = "DejaVuSans"
    try:
        fn = _register_pdf_font()
    except Exception:
        pass

    buf = tempfile.SpooledTemporaryFile()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            rightMargin=10*mm, leftMargin=10*mm,
                            topMargin=12*mm, bottomMargin=12*mm,
                            title=title)
    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "Base", parent=styles["Normal"], fontName=fn, fontSize=7.5,
        leading=9, wordWrap="CJK")
    hstyle = ParagraphStyle(
        "H", parent=base,
        fontName="DejaVuSans-Bold" if fn == "DejaVuSans" else fn,
        fontSize=8, leading=10, textColor=colors.white)
    title_style = ParagraphStyle(
        "Title", parent=base, fontSize=14, leading=18, spaceAfter=8)

    story = [Paragraph(title, title_style)]

    headers, rows = _build_report_rows(data)

    # сокращаем длинные ячейки
    def short(v, n=60):
        s = str(v or "").replace("\n", " ").strip()
        return s if len(s) <= n else s[:n-1] + "…"

    table_data = [[Paragraph(h.replace(":", ":<br/>"), hstyle) for h in headers]]
    for r in rows:
        table_data.append([Paragraph(_xml_escape(short(v)), base) for v in r])

    col_w = [28*mm, 14*mm, 20*mm, 11*mm, 12*mm] + [18*mm]*(len(headers)-5)
    t = Table(table_data, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2B4C8F")),
        ("GRID", (0, 0), (-1, -1), 0.2, colors.HexColor("#9AA7BC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EEF2F8")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(t)
    doc.build(story)
    buf.seek(0)
    return buf


def _build_employee_pdf(data, title):
    """PDF по одному сотруднику: карточка + полугодия + встречи."""
    fn = "DejaVuSans"
    try:
        fn = _register_pdf_font()
    except Exception:
        pass

    buf = tempfile.SpooledTemporaryFile()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=14*mm, leftMargin=14*mm,
                            topMargin=14*mm, bottomMargin=14*mm, title=title)
    styles = getSampleStyleSheet()
    base = ParagraphStyle("Base", parent=styles["Normal"], fontName=fn,
                          fontSize=9, leading=12, wordWrap="CJK")
    h2 = ParagraphStyle("H2", parent=base, fontSize=12, leading=15, spaceAfter=6, spaceBefore=10)
    h3 = ParagraphStyle("H3", parent=base, fontSize=10, leading=13, spaceAfter=4, spaceBefore=6)
    title_style = ParagraphStyle("Title", parent=base, fontSize=16, leading=20, spaceAfter=10)

    story = [Paragraph(_xml_escape(title), title_style)]

    item = data[0]
    # стиль подписей (label) и значений — ОБЯЗАТЕЛЬНО с кириллическим шрифтом
    label_st = ParagraphStyle("Lbl", parent=base, fontName=fn, fontSize=9,
                              leading=11, textColor=colors.HexColor("#33415C"))
    val_st = ParagraphStyle("Val", parent=base, fontName=fn, fontSize=9, leading=11)
    story.append(Table(
        [[Paragraph("Сотрудник", label_st), Paragraph(_xml_escape(item["name"] or "—"), val_st)],
         [Paragraph("Отдел", label_st), Paragraph(_xml_escape(item["department"] or "—"), val_st)],
         [Paragraph("Должность", label_st), Paragraph(_xml_escape(item["position"] or "—"), val_st)],
         [Paragraph("Зарплата", label_st), Paragraph(_xml_escape(item["salary"] or "—"), val_st)],
         [Paragraph("Дата приёма", label_st), Paragraph(_xml_escape(_fmt_date(item["hire_date"]) or "—"), val_st)]],
        colWidths=[40*mm, 130*mm],
        style=TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#9AA7BC")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EEF2F8")),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#F8FAFD")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]),
    ))

    labels = {"1H": "I полугодие", "2H": "II полугодие"}
    sem_cols = [("goals_employee", "Цели сотрудника"),
                ("proposals_manager", "Мои предложения"),
                ("wishes_employee", "Пожелания сотрудника"),
                ("comments", "Мои комментарии"),
                ("colleagues_feedback", "Отзывы коллег")]
    for code in SEMESTERS:
        story.append(Paragraph(labels[code], h2))
        rec = item["semesters"].get(code) or {}
        for field, label in sem_cols:
            val = (rec.get(field) or "").strip()
            story.append(Paragraph(f"<b>{label}:</b>", h3))
            story.append(Paragraph(_xml_escape(val) if val else "—", base))
            story.append(Spacer(1, 3))

    story.append(PageBreak())
    story.append(Paragraph("Встречи 1-на-1", h2))
    if item["meetings"]:
        for m in item["meetings"]:
            story.append(Paragraph(f"<b>{_xml_escape(_fmt_date(m['date']))}</b>", h3))
            story.append(Paragraph(_xml_escape(m["summary"]) if m["summary"] else "—", base))
            story.append(Spacer(1, 5))
    else:
        story.append(Paragraph("Встреч не было.", base))

    doc.build(story)
    buf.seek(0)
    return buf


@app.route("/report")
@login_required
def report_all():
    """Скачать отчёт по всем сотрудникам за выбранный год (Excel или PDF)."""
    db = get_db()
    year = request.args.get("year", type=int) or datetime.now().year
    fmt = request.args.get("format", "xlsx").lower().strip()
    data = _report_data(db, eid=None, year=year)

    if fmt == "pdf":
        if not HAS_PDF:
            flash("Модуль reportlab недоступен на сервере", "error")
            return redirect(url_for("index"))
        buf = _build_report_pdf(data, f"TeamBook — отчёт за {year} год")
        name = f"teambook_report_{year}.pdf"
        mimetype = "application/pdf"
        src = buf
    else:
        if not HAS_EXCEL:
            flash("Модуль openpyxl недоступен на сервере", "error")
            return redirect(url_for("index"))
        headers, rows = _build_report_rows(data)
        wb = Workbook(); ws = wb.active; ws.title = f"Отчёт {year}"
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(start_color="2B4C8F", end_color="2B4C8F", fill_type="solid")
        for r in rows:
            ws.append(r)
        widths = [28, 18, 26, 14, 12] + [18] * (len(headers) - 5)
        for idx, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(idx)].width = w
        for cell in ws[1]:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.freeze_panes = "A2"
        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        wb.save(tmp.name); tmp.close()
        name = f"teambook_report_{year}.xlsx"
        mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        src = tmp.name

    return _send_file_cleanup(src, name, mimetype)


@app.route("/employee/<int:eid>/report")
@login_required
def report_employee(eid):
    """Скачать отчёт по одному сотруднику за выбранный год (Excel или PDF)."""
    db = get_db()
    year = request.args.get("year", type=int) or datetime.now().year
    fmt = request.args.get("format", "xlsx").lower().strip()
    data = _report_data(db, eid=eid, year=year)

    safe = None
    if fmt == "pdf":
        if not HAS_PDF:
            flash("Модуль reportlab недоступен на сервере", "error")
            return redirect(url_for("employee_view", eid=eid))
        buf = _build_employee_pdf(data, f"TeamBook — {data[0]['name']} · {year}")
        import re
        safe = re.sub(r"[^\w\- ]", "", data[0]["name"]) or "employee"
        name = f"teambook_{safe}_{year}.pdf"
        mimetype = "application/pdf"
        src = buf
    else:
        if not HAS_EXCEL:
            flash("Модуль openpyxl недоступен на сервере", "error")
            return redirect(url_for("employee_view", eid=eid))
        headers, rows = _build_report_rows(data)
        wb = Workbook(); ws = wb.active; ws.title = _safe_sheet_name(data[0]["name"])
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(start_color="2B4C8F", end_color="2B4C8F", fill_type="solid")
        for r in rows:
            ws.append(r)
        widths = [28, 18, 26, 14, 12] + [18] * (len(headers) - 5)
        for idx, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(idx)].width = w
        for cell in ws[1]:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.freeze_panes = "A2"
        ws2 = wb.create_sheet("Встречи 1-на-1")
        ws2.append(["Дата", "Итоги встречи"])
        for cell in ws2[1]:
            cell.font = Font(bold=True)
        for m in data[0]["meetings"]:
            ws2.append([_xlsx_safe(_fmt_date(m["date"])), _xlsx_safe(m["summary"])])
        ws2.column_dimensions["A"].width = 14
        ws2.column_dimensions["B"].width = 90
        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        wb.save(tmp.name); tmp.close()
        import re
        safe = re.sub(r"[^\w\- ]", "", data[0]["name"]) or "employee"
        name = f"teambook_{safe}_{year}.xlsx"
        mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        src = tmp.name

    return _send_file_cleanup(src, name, mimetype)


# --------------------------------------------------------------------------- #
# Канбан-доска и гант
# --------------------------------------------------------------------------- #
def _task_members_map(db):
    """{task_id: [{'id','name','dept'}]} — все исполнители задач (с отделом)."""
    rows = db.execute(
        """SELECT m.task_id, e.id AS eid, e.name, d.name AS dept
           FROM kb_task_members m
           JOIN employees e ON e.id = m.employee_id
           LEFT JOIN departments d ON d.id = e.department_id
           ORDER BY e.name COLLATE NOCASE""").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["task_id"], []).append({
            "id": r["eid"], "name": r["name"], "dept": r["dept"] or None})
    return out


def _board_ctx(db, month=None, year=None):
    """Общий контекст для доски: столбцы, задачи, сотрудники, гант-сетка.

    Возвращает dict с колонками канбана (активные задачи по столбцам),
    счётчиками архива/корзины, списком активных сотрудников и данными ганта
    на выбранный месяц.
    """
    # на канбане — только обычные столбцы (kind='kanban', включая Бэклог)
    columns = db.execute(
        "SELECT * FROM kb_columns WHERE kind='kanban' ORDER BY sort_order, id"
    ).fetchall()

    # только активные задачи (не в архиве и не в корзине)
    tasks = db.execute(
        """SELECT t.* FROM kb_tasks t
           WHERE t.archived_at = '' AND t.deleted_at = ''
           ORDER BY t.id DESC""").fetchall()
    members = _task_members_map(db)

    # сегодняшняя дата для подсветки/сортировки задач по сроку
    today_s = datetime.now().strftime("%Y-%m-%d")
    col_tasks = {c["id"]: [] for c in columns}
    for t in tasks:
        col_tasks.setdefault(t["column_id"], []).append(t)
    # сортировка задач внутри столбца: просроченные первыми, затем по сроку (без срока — в конец)
    def _sort_key(t):
        due = t["due_date"] or "9999-12-31"
        return (0 if t["due_date"] and t["due_date"] < today_s else 1, due)
    for cid in col_tasks:
        col_tasks[cid].sort(key=_sort_key)
    cols_view = list(columns)

    employees = db.execute(
        "SELECT id, name FROM employees WHERE active = 1 ORDER BY name COLLATE NOCASE"
    ).fetchall()

    # счётчики архива и корзины
    trash_cnt = db.execute(
        "SELECT COUNT(*) c FROM kb_tasks WHERE deleted_at != ''").fetchone()["c"]
    archive_cnt = db.execute(
        "SELECT COUNT(*) c FROM kb_tasks WHERE archived_at != '' AND deleted_at = ''"
    ).fetchone()["c"]

    # --- гант: календарная сетка месяца, задачи по дням ---
    from calendar import monthrange, month_name as _mn
    now = datetime.now()
    month = month or now.month
    year = year or now.year
    if month < 1: month, year = 12, year - 1
    if month > 12: month, year = 1, year + 1
    ndays = monthrange(year, month)[1]
    first_wd = monthrange(year, month)[0]
    def _iso(y, m, d):
        return f"{y:04d}-{m:02d}-{d:02d}"
    mon_start = _iso(year, month, 1)
    mon_end = _iso(year, month, ndays)
    today_iso = now.strftime("%Y-%m-%d")

    # задачи на календарь: с датами (исполнитель не обязателен для отображения)
    gantt_tasks = [
        t for t in tasks if t["start_date"] and t["due_date"]
    ]
    tasks_by_day = {}
    for t in gantt_tasks:
        if t["due_date"] < mon_start or t["start_date"] > mon_end:
            continue
        ef = max(t["start_date"], mon_start)
        ee = min(t["due_date"], mon_end)
        d = datetime.strptime(ef, "%Y-%m-%d")
        de = datetime.strptime(ee, "%Y-%m-%d")
        emp_names = ", ".join(m["name"] for m in members.get(t["id"], []))
        day = d
        while day <= de:
            iso = day.strftime("%Y-%m-%d")
            tasks_by_day.setdefault(iso, []).append({
                "id": t["id"],
                "title": t["title"],
                "emp": emp_names or "—",
                "overdue": t["due_date"] < today_iso,
                "start": t["start_date"],
                "due": t["due_date"],
            })
            day += timedelta(days=1)

    cells = [None] * first_wd
    for d in range(1, ndays + 1):
        iso = _iso(year, month, d)
        cells.append({
            "num": d,
            "iso": iso,
            "weekend": (first_wd + d - 1) % 7 >= 5,
            "today": iso == today_iso,
            "tasks": tasks_by_day.get(iso, []),
        })
    while len(cells) % 7 != 0:
        cells.append(None)
    weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
    return {
        "columns": columns,
        "col_tasks": col_tasks,
        "cols_view": cols_view,
        "employees": employees,
        "members": members,
        "gantt_weeks": weeks,
        "trash_cnt": trash_cnt,
        "archive_cnt": archive_cnt,
        "gmonth": month,
        "gyear": year,
        "gmonth_name": _mn[month],
        "gnext": (month + 1, year) if month < 12 else (1, year + 1),
        "gprev": (month - 1, year) if month > 1 else (12, year - 1),
        "today_iso": today_iso,
    }


def _purge_stale_trash(db, now_utc=None):
    """Окончательно удаляет задачи из корзины старше 30 дней."""
    # SQLite datetime('now') хранит UTC; порог считаем в той же шкале времени.
    now_utc = now_utc or datetime.utcnow()
    month_ago = (now_utc - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "DELETE FROM kb_tasks WHERE deleted_at != '' AND deleted_at < ?",
        (month_ago,))



@app.route("/board")
@login_required
def board():
    db = get_db()
    try:
        month = int(request.args.get("month", 0)) or None
    except (TypeError, ValueError):
        month = None
    try:
        year = int(request.args.get("year", 0)) or None
    except (TypeError, ValueError):
        year = None
    ctx = _board_ctx(db, month=month, year=year)
    return render_template("board.html", **ctx)



@app.route("/board/column/add", methods=["POST"])
@login_required
def board_column_add():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Укажите название столбца", "error")
        return redirect(url_for("board"))
    db = get_db()
    row = db.execute("SELECT COALESCE(MAX(sort_order), -1) m FROM kb_columns").fetchone()
    db.execute("INSERT INTO kb_columns (name, sort_order) VALUES (?, ?)",
               (name, row["m"] + 1))
    db.commit()
    flash(f"Столбец «{name}» создан", "ok")
    return redirect(url_for("board"))


@app.route("/board/column/<int:cid>/rename", methods=["POST"])
@login_required
def board_column_rename(cid):
    name = request.form.get("name", "").strip()
    db = get_db()
    col = db.execute("SELECT * FROM kb_columns WHERE id=?", (cid,)).fetchone()
    if not col:
        flash("Столбец не найден", "error")
    elif col["locked"]:
        flash("Системный столбец нельзя переименовать", "error")
    elif not name:
        flash("Пустое имя", "error")
    else:
        db.execute("UPDATE kb_columns SET name=? WHERE id=?", (name, cid))
        db.commit()
        flash("Столбец переименован", "ok")
    return redirect(url_for("board"))


@app.route("/board/column/<int:cid>/delete", methods=["POST"])
@login_required
def board_column_delete(cid):
    db = get_db()
    col = db.execute("SELECT * FROM kb_columns WHERE id=?", (cid,)).fetchone()
    if not col:
        flash("Столбец не найден", "error")
    elif col["locked"]:
        flash("Системный столбец нельзя удалить", "error")
    else:
        # блокируем удаление, если в столбце есть ЛЮБЫЕ задачи (включая архивные и
        # в корзине), иначе удаление выставит им column_id=NULL по FK и после
        # восстановления карточка станет невидимой
        cnt = db.execute("SELECT COUNT(*) c FROM kb_tasks WHERE column_id=?",
                         (cid,)).fetchone()["c"]
        if cnt:
            flash(f"Сначала перенесите или удалите все задачи из этого столбца ({cnt})", "error")
        else:
            db.execute("DELETE FROM kb_columns WHERE id=?", (cid,))
            db.commit()
            flash("Столбец удалён", "ok")
    return redirect(url_for("board"))


@app.route("/board/column/<int:cid>/move", methods=["POST"])
@login_required
def board_column_move(cid):
    """Переставить столбец: перед столбцом `before` (или в конец, если не задан)."""
    db = get_db()
    col = db.execute(
        "SELECT id FROM kb_columns WHERE id=? AND kind='kanban'", (cid,)
    ).fetchone()
    if not col:
        return "", 204
    before_id = _clean_int(request.form.get("before"))
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM kb_columns WHERE kind='kanban' ORDER BY sort_order, id"
    ).fetchall()]
    if cid in ids:
        ids.remove(cid)
    if before_id and before_id in ids:
        ids.insert(ids.index(before_id), cid)
    else:
        ids.append(cid)
    for i, c in enumerate(ids):
        db.execute("UPDATE kb_columns SET sort_order=? WHERE id=?", (i * 10, c))
    db.commit()
    return "", 204


def _parse_members(form_getlist):
    """Список id исполнителей из формы (multi select / чекбоксов)."""
    out = []
    for v in form_getlist:
        if str(v).strip().isdigit():
            out.append(int(v))
    return sorted(set(out))


def _valid_kanban_column(db, raw):
    cid = _clean_int(raw)
    if not cid:
        return None
    row = db.execute(
        "SELECT id FROM kb_columns WHERE id=? AND kind='kanban'", (cid,)
    ).fetchone()
    return cid if row else None


@app.route("/board/task/add", methods=["POST"])
@login_required
def board_task_add():
    title = request.form.get("title", "").strip()
    if not title:
        flash("Укажите название задачи", "error")
        return redirect(url_for("board"))
    db = get_db()
    # задача создаётся в столбце из формы (если передан валидный id),
    # иначе — в первый обычный столбец канбана
    raw_cid = request.form.get("column_id", "").strip()
    cid = _valid_kanban_column(db, raw_cid)
    if raw_cid and cid is None:
        abort(400, "Некорректная колонка канбана")
    if not cid:
        first = db.execute(
            "SELECT id FROM kb_columns WHERE kind='kanban' "
            "ORDER BY sort_order, id LIMIT 1").fetchone()
        cid = first["id"] if first else None
    start_date = request.form.get("start_date", "").strip()
    due_date = request.form.get("due_date", "").strip()
    if (not _valid_iso_date(start_date) or not _valid_iso_date(due_date)
            or (start_date and due_date and start_date > due_date)):
        abort(400, "Некорректный диапазон дат")
    cur = db.execute(
        "INSERT INTO kb_tasks (title, column_id, description, start_date, due_date) "
        "VALUES (?,?,?,?,?)",
        (title, cid,
         request.form.get("description", ""),
         start_date, due_date),
    )
    tid = cur.lastrowid
    for eid in _parse_members(request.form.getlist("employee_id")):
        db.execute(
            "INSERT OR IGNORE INTO kb_task_members (task_id, employee_id) VALUES (?,?)",
            (tid, eid))
    db.commit()
    flash("Задача добавлена", "ok")
    return redirect(url_for("board"))


@app.route("/board/task/<int:tid>/edit", methods=["POST"])
@login_required
def board_task_edit(tid):
    db = get_db()
    t = db.execute("SELECT * FROM kb_tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        abort(404)
    title = request.form.get("title", "").strip()
    if not title:
        flash("Название задачи не может быть пустым", "error")
        return redirect(url_for("board"))
    col = request.form.get("column_id", "").strip()
    column_id = _valid_kanban_column(db, col)
    if col and column_id is None:
        abort(400, "Некорректная колонка канбана")
    start_date = request.form.get("start_date", "").strip()
    due_date = request.form.get("due_date", "").strip()
    if (not _valid_iso_date(start_date) or not _valid_iso_date(due_date)
            or (start_date and due_date and start_date > due_date)):
        abort(400, "Некорректный диапазон дат")
    db.execute(
        "UPDATE kb_tasks SET title=?, column_id=?, description=?, "
        "start_date=?, due_date=?, updated_at=datetime('now') WHERE id=?",
        (title, column_id,
         request.form.get("description", ""),
         start_date, due_date, tid),
    )
    # заменить список исполнителей
    db.execute("DELETE FROM kb_task_members WHERE task_id=?", (tid,))
    for eid in _parse_members(request.form.getlist("employee_id")):
        db.execute(
            "INSERT OR IGNORE INTO kb_task_members (task_id, employee_id) VALUES (?,?)",
            (tid, eid))
    db.commit()
    flash("Задача обновлена", "ok")
    return redirect(url_for("board"))


@app.route("/board/task/<int:tid>/card", methods=["GET"])
@login_required
def board_task_card(tid):
    """Модалка карточки (Rendered фрагмент) — клик по карточке на доске."""
    db = get_db()
    t = db.execute("SELECT * FROM kb_tasks WHERE id=?", (tid,)).fetchone()
    if not t or t["deleted_at"]:
        abort(404)
    employees = db.execute(
        "SELECT id, name FROM employees WHERE active = 1 ORDER BY name COLLATE NOCASE"
    ).fetchall()
    columns = db.execute(
        "SELECT * FROM kb_columns ORDER BY sort_order, id").fetchall()
    selected = {r["employee_id"] for r in db.execute(
        "SELECT employee_id FROM kb_task_members WHERE task_id=?", (tid,))}
    return render_template(
        "_task_modal.html",
        t=t, employees=employees, columns=columns, selected=selected,
        in_archive=bool(t["archived_at"]),
    )


@app.route("/board/task/<int:tid>/move", methods=["POST"])
@login_required
def board_task_move(tid):
    """Drag&drop карточки между столбцами канбана."""
    db = get_db()
    if not db.execute("SELECT 1 FROM kb_tasks WHERE id=? AND deleted_at=''",
                      (tid,)).fetchone():
        abort(404)
    col = request.form.get("column_id", "").strip()
    column_id = _valid_kanban_column(db, col)
    if not column_id:
        abort(400, "Некорректная колонка канбана")
    db.execute("UPDATE kb_tasks SET column_id=?, updated_at=datetime('now') WHERE id=?",
               (column_id, tid))
    db.commit()
    return "", 204



@app.route("/board/task/<int:tid>/archive", methods=["POST"])
@login_required
def board_task_archive(tid):
    """Перетащили в архив."""
    db = get_db()
    db.execute(
        "UPDATE kb_tasks SET archived_at=datetime('now'), deleted_at='', "
        "updated_at=datetime('now') WHERE id=?",
        (tid,))
    db.commit()
    return "", 204


@app.route("/board/task/<int:tid>/trash", methods=["POST"])
@login_required
def board_task_trash(tid):
    """Перетащили в корзину (мягкое удаление)."""
    db = get_db()
    db.execute(
        "UPDATE kb_tasks SET deleted_at=datetime('now'), archived_at='', "
        "updated_at=datetime('now') WHERE id=?",
        (tid,))
    db.commit()
    return "", 204


@app.route("/board/trash/purge-all", methods=["POST"])
@login_required
def board_trash_clear():
    db = get_db()
    db.execute("DELETE FROM kb_tasks WHERE deleted_at != ''")
    db.commit()
    flash("Корзина очищена", "ok")
    return redirect(url_for("board_trash"))


@app.route("/board/archive")
@login_required
def board_archive():
    db = get_db()
    items = db.execute(
        """SELECT t.*, (SELECT COUNT(*) FROM kb_task_members m WHERE m.task_id=t.id) AS n
           FROM kb_tasks t WHERE t.archived_at != '' AND t.deleted_at = ''
           ORDER BY t.archived_at DESC, t.id DESC""").fetchall()
    members = _task_members_map(db)
    employees = db.execute(
        "SELECT id, name FROM employees WHERE active = 1 ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return render_template("board_archive.html", items=items, members=members,
                           employees=employees)


@app.route("/board/trash")
@login_required
def board_trash():
    db = get_db()
    items = db.execute(
        """SELECT t.*, (SELECT COUNT(*) FROM kb_task_members m WHERE m.task_id=t.id) AS n
           FROM kb_tasks t WHERE t.deleted_at != ''
           ORDER BY t.deleted_at DESC, t.id DESC""").fetchall()
    members = _task_members_map(db)
    return render_template("board_trash.html", items=items, members=members)


@app.route("/board/task/<int:tid>/restore", methods=["POST"])
@login_required
def board_task_restore(tid):
    """Вернуть задачу из архива/корзины на доску."""
    db = get_db()
    t = db.execute("SELECT * FROM kb_tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        abort(404)
    # если колонка отсутствует/удалена — вернуть задачу в доступный столбец
    column_id = _valid_kanban_column(db, t["column_id"])
    if column_id is None:
        first = db.execute(
            "SELECT id FROM kb_columns WHERE kind='kanban' "
            "ORDER BY sort_order, id LIMIT 1").fetchone()
        column_id = first["id"] if first else None
    db.execute(
        "UPDATE kb_tasks SET archived_at='', deleted_at='', column_id=?, "
        "updated_at=datetime('now') WHERE id=?",
        (column_id, tid))
    db.commit()
    flash("Задача возвращена", "ok")
    return redirect(request.referrer or url_for("board"))


@app.route("/board/task/<int:tid>/purge", methods=["POST"])
@login_required
def board_task_purge(tid):
    """Окончательное удаление задачи (из корзины)."""
    db = get_db()
    db.execute("DELETE FROM kb_tasks WHERE id=?", (tid,))
    db.commit()
    flash("Задача удалена безвозвратно", "ok")
    return redirect(request.referrer or url_for("board"))


# --------------------------------------------------------------------------- #
# Личный todo руководителя (бэклог → на сегодня)
# --------------------------------------------------------------------------- #
@app.route("/todo")
@login_required
def todo_page():
    db = get_db()
    today_s = date.today().isoformat()
    # (архив хранит выполненные за любой день — история не чистится автоматически)
    backlog = db.execute(
        "SELECT * FROM todo_items WHERE status='backlog' ORDER BY sort_order, id"
    ).fetchall()
    quad_tasks = {}
    for q in QUADRANTS:
        quad_tasks[q["key"]] = db.execute(
            "SELECT * FROM todo_items WHERE status=? ORDER BY sort_order, id",
            (q["key"],)).fetchall()
    archive = db.execute(
        "SELECT * FROM todo_items WHERE status='done' AND done_date=? "
        "ORDER BY id DESC", (today_s,)
    ).fetchall()
    tags = sorted({r["tag"] for r in db.execute(
        "SELECT tag FROM todo_items WHERE tag != ''").fetchall()})
    return render_template(
        "todo.html", backlog=backlog, quadrants=QUADRANTS,
        quad_tasks=quad_tasks, archive=archive, today_s=today_s,
        all_tags=tags)


# Квадранты Эйзенхауэра в личном todo (важно × срочно)
QUADRANTS = [
    {"key": "q_iu", "title": "Важно · Срочно",      "icon": "🔴", "cls": "q-iu", "action": "Сделать"},
    {"key": "q_in", "title": "Важно · Не срочно",   "icon": "🟠", "cls": "q-in", "action": "Запланировать"},
    {"key": "q_nu", "title": "Не важно · Срочно",   "icon": "🟡", "cls": "q-nu", "action": "Делегировать"},
    {"key": "q_nn", "title": "Не важно · Не срочно", "icon": "🟢", "cls": "q-nn", "action": "Удалить"},
]


@app.route("/todo/archive")
@login_required
def todo_archive():
    """Архив личного todo: выполненные за выбранный день (по умолчанию — сегодня)."""
    db = get_db()
    today_s = date.today().isoformat()
    day = request.args.get("date", "").strip() or today_s
    if day > today_s:
        day = today_s
    items = db.execute(
        "SELECT * FROM todo_items WHERE status='done' AND done_date=? "
        "ORDER BY id DESC", (day,)
    ).fetchall()
    prev_d = next_d = None
    try:
        from datetime import timedelta
        d = datetime.strptime(day, "%Y-%m-%d").date()
        if d - timedelta(days=1) >= datetime(2026, 1, 1).date():
            prev_d = (d - timedelta(days=1)).isoformat()
        if d < date.today():
            next_d = (d + timedelta(days=1)).isoformat()
    except Exception:
        pass
    return render_template(
            "todo_archive.html", archive=items, day=day,
            prev=prev_d, next=next_d, todays=today_s)


@app.route("/todo/add", methods=["POST"])
@login_required
def todo_add():
    title = request.form.get("title", "").strip()
    if title:
        db = get_db()
        # новые попадают в конец бэклога
        nxt = db.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM todo_items "
            "WHERE status='backlog'").fetchone()["n"]
        db.execute("INSERT INTO todo_items (title, status, sort_order) VALUES (?, 'backlog', ?)",
                   (title, nxt))
        db.commit()
        flash("Добавлено в бэклог", "ok")
    return redirect(url_for("todo_page"))


@app.route("/todo/<int:todo>/edit", methods=["POST"])
@login_required
def todo_edit(todo):
    """Обновить задачу в личном todo: заголовок, срок, тег."""
    db = get_db()
    row = db.execute("SELECT * FROM todo_items WHERE id=?", (todo,)).fetchone()
    if row:
        title = request.form.get("title", "").strip() or row["title"]
        due = request.form.get("due_date", "").strip()
        if not _valid_iso_date(due):
            abort(400, "Некорректная дата срока")
        tag = request.form.get("tag", "").strip()
        db.execute(
            "UPDATE todo_items SET title=?, due_date=?, tag=? WHERE id=?",
            (title, due, tag, todo))
        db.commit()
        flash("Задача обновлена", "ok")
    return redirect(url_for("todo_page"))


@app.route("/todo/<int:todo>/card")
@login_required
def todo_card(todo):
    """Фрагмент модалки редактирования задачи личного todo."""
    db = get_db()
    t = db.execute("SELECT * FROM todo_items WHERE id=?", (todo,)).fetchone()
    if not t:
        return "", 404
    tags = [r["tag"] for r in db.execute(
        "SELECT DISTINCT tag FROM todo_items WHERE tag != '' ORDER BY tag") if r["tag"]]
    return render_template("_todo_modal.html", t=t, tags=tags)


@app.route("/todo/<int:todo>/move", methods=["POST"])
@login_required
def todo_move(todo):
    """Переставить задачу между бэклогом и квадрантами матрицы (drag&drop)."""
    target = request.form.get("status", "")
    valid = {"backlog"} | {q["key"] for q in QUADRANTS}
    if target not in valid:
        return "", 204
    db = get_db()
    nxt = db.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM todo_items WHERE status=?",
        (target,)).fetchone()["n"]
    db.execute(
        "UPDATE todo_items SET status=?, assigned_date='', done_date='', sort_order=? WHERE id=?",
        (target, nxt, todo))
    db.commit()
    return "", 204


@app.route("/todo/<int:todo>/done", methods=["POST"])
@login_required
def todo_done(todo):
    db = get_db()
    db.execute(
        "UPDATE todo_items SET status='done', done_date=? WHERE id=?",
        (date.today().isoformat(), todo))
    db.commit()
    flash("Сделано ✓ — в архиве", "ok")
    return redirect(url_for("todo_page"))


@app.route("/todo/<int:todo>/delete", methods=["POST"])
@login_required
def todo_delete(todo):
    db = get_db()
    db.execute("DELETE FROM todo_items WHERE id=?", (todo,))
    db.commit()
    flash("Удалено", "ok")
    return redirect(url_for("todo_page"))


@app.route("/todo/<int:todo>/delegate", methods=["POST"])
@login_required
def todo_delegate(todo):
    """Делегировать: личная задача → канбан (в первый обычный столбец)."""
    db = get_db()
    t = db.execute("SELECT * FROM todo_items WHERE id=?", (todo,)).fetchone()
    if t:
        first = db.execute(
            "SELECT id FROM kb_columns WHERE kind='kanban' "
            "ORDER BY sort_order, id LIMIT 1").fetchone()
        db.execute(
            "INSERT INTO kb_tasks (title, column_id, description, due_date) "
            "VALUES (?,?,?,?)",
            (t["title"], first["id"] if first else None,
             "Делегировано из личного todo", t["due_date"] or ""))
        db.execute("DELETE FROM todo_items WHERE id=?", (todo,))
        db.commit()
        flash("Отправлено на канбан", "ok")
    return redirect(url_for("todo_page"))


def _todo_move(db, todo, up=True):
    t = db.execute("SELECT * FROM todo_items WHERE id=?", (todo,)).fetchone()
    if not t:
        return
    op = "<" if up else ">"
    order = "DESC" if up else "ASC"
    other = db.execute(
        "SELECT * FROM todo_items WHERE status=? AND sort_order %s ? "
        "ORDER BY sort_order %s LIMIT 1" % (op, order),
        (t["status"], t["sort_order"])).fetchone()
    if other:
        db.execute("UPDATE todo_items SET sort_order=? WHERE id=?",
                   (other["sort_order"], t["id"]))
        db.execute("UPDATE todo_items SET sort_order=? WHERE id=?",
                   (t["sort_order"], other["id"]))
        db.commit()


@app.route("/todo/<int:todo>/up", methods=["POST"])
@login_required
def todo_up(todo):
    db = get_db()
    _todo_move(db, todo, up=True)
    db.commit()
    return redirect(url_for("todo_page"))


@app.route("/todo/<int:todo>/down", methods=["POST"])
@login_required
def todo_down(todo):
    db = get_db()
    _todo_move(db, todo, up=False)
    db.commit()
    return redirect(url_for("todo_page"))


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    init_db()
    if not ADMIN_PASSWORD:
        ADMIN_PASSWORD = secrets.token_urlsafe(12)
        print("=" * 60)
        print("[TeamBook] Пароль не задан (HR_PASSWORD).")
        print(f"[TeamBook] Сгенерирован временный: {ADMIN_PASSWORD}")
        print("=" * 60)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")