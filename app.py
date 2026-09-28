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
import shutil
import tempfile
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, request, redirect, url_for, render_template, session, flash, abort, g,
    send_file,
)

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from openpyxl.comments import Comment
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
    from reportlab.lib.enums import TA_CENTER
    HAS_PDF = True
except Exception:
    HAS_PDF = False

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("HR_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "hr_notes.db")

ADMIN_PASSWORD = os.environ.get("HR_PASSWORD", "")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("HR_SECRET_KEY", secrets.token_hex(32))

SEMESTERS = {"1H": "I полугодие (янв–июн)", "2H": "II полугодие (июл–дек)"}

# --------------------------------------------------------------------------- #
# БД
# --------------------------------------------------------------------------- #
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


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
    semester          TEXT NOT NULL,
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
    start_date   TEXT DEFAULT '',   -- дата начала (ISO YYYY-MM-DD)
    due_date     TEXT DEFAULT '',   -- срок/дата окончания
    archived_at  TEXT DEFAULT '',   -- не пусто = в архиве
    deleted_at   TEXT DEFAULT '',   -- не пусто = в корзине
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
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

-- Личный todo руководителя: бэклог задач и выбор «на сегодня».
-- status: 'backlog' (в отложенном бэклоге) | 'today' (назначено на сегодня)
-- assigned_date: дата, на которую задача назначена «на сегодня» (для авто-сброса)
CREATE TABLE IF NOT EXISTS todo_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'backlog',
    sort_order    INTEGER NOT NULL DEFAULT 0,
    assigned_date TEXT DEFAULT '',
    done_date     TEXT DEFAULT '',
    created_at    TEXT DEFAULT (datetime('now'))
);
"""


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
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

    # Миграция канбана: колонки архива/корзины + many-to-many исполнители.
    kb_cols = {r[1] for r in db.execute("PRAGMA table_info(kb_tasks)").fetchall()}
    if "archived_at" not in kb_cols:
        db.execute("ALTER TABLE kb_tasks ADD COLUMN archived_at TEXT DEFAULT ''")
    if "deleted_at" not in kb_cols:
        db.execute("ALTER TABLE kb_tasks ADD COLUMN deleted_at TEXT DEFAULT ''")
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
            db.execute(
                "UPDATE kb_tasks SET column_id=? WHERE column_id=? AND "
                "archived_at='' AND deleted_at=''",
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
    # (kind='emi1'..'emi4'), убираем их. Их задачи уже перенесены в Бэклог
    # прошлой миграцией; просто удаляем ставшие лишними системные столбцы.
    for ec in db.execute("SELECT id FROM kb_columns WHERE kind LIKE 'emi%'").fetchall():
        db.execute("DELETE FROM kb_columns WHERE id=?", (ec["id"],))
        print("[TeamBook] Откат Эйзенхауэра: удалён квадрант-столбец id=%s" % ec["id"])
    # и убираем ставшее ненужным поле emi из задач (если колонка есть)
    if "emi" in kb_cols:
        try:
            db.execute("ALTER TABLE kb_tasks DROP COLUMN emi")
            print("[TeamBook] Откат Эйзенхауэра: удалена колонка emi из kb_tasks")
        except Exception:
            pass  # DROP COLUMN может быть недоступен в старых SQLite — колонка останется, код её не использует

    db.commit()
    db.close()


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
    if request.endpoint in ("login", "static") or request.endpoint is None:
        return
    if not session.get("authed"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session["authed"] = True
            return redirect(url_for("index"))
        flash("Неверный пароль", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("authed", None)
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
    overdue, soon = [], []
    for r in rows:
        item = {
            "id": r["id"], "title": r["title"],
            "due_date": r["due_date"], "col_name": r["col_name"] or "—",
            "emp": ", ".join(m["name"] for m in members.get(r["id"], [])),
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
        board_overdue = len(notif["overdue"])
        return {"notifications": notif, "problems_count": n,
                "board_overdue": board_overdue}
    except Exception:
        return {"notifications": None}


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
    stats = {
        "open_tasks": open_tasks,
        "overdue_tasks": overdue_tasks,
        "problems": problems_count,
        "records": records_count,
        "year": this_year,
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
    _p.exists(DB_PATH) or abort(404)
    _, tmp = tempfile.mkstemp(suffix=".db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmp)
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"teambook_backup_{stamp}.db"
    return send_file(tmp, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.sqlite3", max_age=0)


@app.route("/backup/import", methods=["POST"])
@login_required
def backup_import():
    """Восстановить БД из загруженного файла. Текущая БД бэкапируется рядом."""
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

    # Бэкап текущей БД перед перезаписью
    backup_path = DB_PATH + ".pre_restore.bak"
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, backup_path)
    os.replace(upload_path, DB_PATH)

    flash("База восстановлена из файла.", "ok")
    return redirect(url_for("index"))


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
            item["name"], item["department"], item["position"],
            item["salary"], _fmt_date(item["hire_date"]),
        ]
        for code in SEMESTERS:
            rec = item["semesters"].get(code) or {}
            for field in ["goals_employee", "proposals_manager",
                          "wishes_employee", "comments", "colleagues_feedback"]:
                row.append((rec.get(field) or "").strip())
        rows.append(row)
    return headers, rows


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
    import re
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
        table_data.append([Paragraph(short(v), base) for v in r])

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

    story = [Paragraph(title, title_style)]

    item = data[0]
    # стиль подписей (label) и значений — ОБЯЗАТЕЛЬНО с кириллическим шрифтом
    label_st = ParagraphStyle("Lbl", parent=base, fontName=fn, fontSize=9,
                              leading=11, textColor=colors.HexColor("#33415C"))
    val_st = ParagraphStyle("Val", parent=base, fontName=fn, fontSize=9, leading=11)
    story.append(Table(
        [[Paragraph("Сотрудник", label_st), Paragraph(item["name"] or "—", val_st)],
         [Paragraph("Отдел", label_st), Paragraph(item["department"] or "—", val_st)],
         [Paragraph("Должность", label_st), Paragraph(item["position"] or "—", val_st)],
         [Paragraph("Зарплата", label_st), Paragraph(item["salary"] or "—", val_st)],
         [Paragraph("Дата приёма", label_st), Paragraph(_fmt_date(item["hire_date"]) or "—", val_st)]],
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
            story.append(Paragraph(val if val else "—", base))
            story.append(Spacer(1, 3))

    story.append(PageBreak())
    story.append(Paragraph("Встречи 1-на-1", h2))
    if item["meetings"]:
        for m in item["meetings"]:
            story.append(Paragraph(f"<b>{_fmt_date(m['date'])}</b>", h3))
            story.append(Paragraph(m["summary"] or "—", base))
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

    return send_file(src, as_attachment=True, download_name=name, mimetype=mimetype, max_age=0)


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
        wb = Workbook(); ws = wb.active; ws.title = f"{data[0]['name'][:25]}"
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
            ws2.append([_fmt_date(m["date"]), m["summary"]])
        ws2.column_dimensions["A"].width = 14
        ws2.column_dimensions["B"].width = 90
        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        wb.save(tmp.name); tmp.close()
        import re
        safe = re.sub(r"[^\w\- ]", "", data[0]["name"]) or "employee"
        name = f"teambook_{safe}_{year}.xlsx"
        mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        src = tmp.name

    return send_file(src, as_attachment=True, download_name=name, mimetype=mimetype, max_age=0)


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
    # авточистка корзины: раз в месяц удаляем из неё задачи окончательно
    _purge_stale_trash(db)

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


def _purge_stale_trash(db):
    """Окончательно удаляет задачи из корзины старше 30 дней (раз в месяц)."""
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
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
        cnt = db.execute("SELECT COUNT(*) c FROM kb_tasks WHERE column_id=? AND deleted_at=''",
                         (cid,)).fetchone()["c"]
        if cnt:
            flash(f"Сначала перенесите или удалите задачи из «{cnt}» этого столбца", "error")
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
    cid = _clean_int(request.form.get("column_id"))
    if cid and not db.execute(
            "SELECT 1 FROM kb_columns WHERE id=? AND kind='kanban'", (cid,)).fetchone():
        cid = None
    if not cid:
        first = db.execute(
            "SELECT id FROM kb_columns WHERE kind='kanban' "
            "ORDER BY sort_order, id LIMIT 1").fetchone()
        cid = first["id"] if first else None
    cur = db.execute(
        "INSERT INTO kb_tasks (title, column_id, description, start_date, due_date) "
        "VALUES (?,?,?,?,?)",
        (title, cid,
         request.form.get("description", ""),
         request.form.get("start_date", ""),
         request.form.get("due_date", "")),
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
    column_id = int(col) if col.isdigit() else None
    db.execute(
        "UPDATE kb_tasks SET title=?, column_id=?, description=?, "
        "start_date=?, due_date=?, updated_at=datetime('now') WHERE id=?",
        (title, column_id,
         request.form.get("description", ""),
         request.form.get("start_date", ""),
         request.form.get("due_date", ""), tid),
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
    column_id = int(col) if col.isdigit() else None
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
    db.execute(
        "UPDATE kb_tasks SET archived_at='', deleted_at='', "
        "updated_at=datetime('now') WHERE id=?",
        (tid,))
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
    # авто-сброс: невыполненные «на вчера» возвращаются в бэклог
    db.execute(
        "UPDATE todo_items SET status='backlog', assigned_date='' "
        "WHERE status='today' AND assigned_date != '' AND assigned_date != ?",
        (today_s,))
    # архив — только задачи, выполненные сегодня; старьё из него удаляем
    db.execute("DELETE FROM todo_items WHERE status='done' AND done_date != ?", (today_s,))
    db.commit()
    backlog = db.execute(
        "SELECT * FROM todo_items WHERE status='backlog' ORDER BY sort_order, id"
    ).fetchall()
    today = db.execute(
        "SELECT * FROM todo_items WHERE status='today' ORDER BY sort_order, id"
    ).fetchall()
    archive = db.execute(
        "SELECT * FROM todo_items WHERE status='done' AND done_date=? "
        "ORDER BY id DESC", (today_s,)
    ).fetchall()
    return render_template("todo.html", backlog=backlog, today=today, archive=archive)


@app.route("/todo/archive")
@login_required
def todo_archive():
    """Архив личного todo: задачи, выполненные сегодня."""
    db = get_db()
    today_s = date.today().isoformat()
    items = db.execute(
        "SELECT * FROM todo_items WHERE status='done' AND done_date=? "
        "ORDER BY id DESC", (today_s,)
    ).fetchall()
    return render_template("todo_archive.html", archive=items)


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
    """Переименовать задачу в личном todo."""
    title = request.form.get("title", "").strip()
    if title:
        db = get_db()
        db.execute("UPDATE todo_items SET title=? WHERE id=?", (title, todo))
        db.commit()
        flash("Задача обновлена", "ok")
    return redirect(url_for("todo_page"))


@app.route("/todo/<int:todo>/today", methods=["POST"])
@login_required
def todo_today(todo):
    db = get_db()
    # решает сам руководитель, mark как "на сегодня"
    nxt = db.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM todo_items "
        "WHERE status='today'").fetchone()["n"]
    db.execute(
        "UPDATE todo_items SET status='today', assigned_date=?, sort_order=? WHERE id=?",
        (date.today().isoformat(), nxt, todo))
    db.commit()
    flash("Задача на сегодня", "ok")
    return redirect(url_for("todo_page"))


@app.route("/todo/<int:todo>/backlog", methods=["POST"])
@login_required
def todo_backlog(todo):
    db = get_db()
    nxt = db.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM todo_items "
        "WHERE status='backlog'").fetchone()["n"]
    db.execute(
        "UPDATE todo_items SET status='backlog', assigned_date='', sort_order=? WHERE id=?",
        (nxt, todo))
    db.commit()
    flash("Возвращено в бэклог", "ok")
    return redirect(url_for("todo_page"))


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
            "INSERT INTO kb_tasks (title, column_id, description) VALUES (?,?,?)",
            (t["title"], first["id"] if first else None,
             "Делегировано из личного todo"))
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
        print(f"[TeamBook] Пароль не задан (HR_PASSWORD).")
        print(f"[TeamBook] Сгенерирован временный: {ADMIN_PASSWORD}")
        print("=" * 60)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")