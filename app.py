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
from datetime import datetime
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
         "department": r["department"], "salary": r["salary"]}
        for r in rows
    ]

    # Поиск по имени/фамилии (регистронезависимо, поддержка кириллицы)
    if q:
        ql = q.lower().replace("ё", "е")
        employees = [e for e in employees
                     if ql in e["name"].lower().replace("ё", "е")]

    return render_template(
        "index.html",
        employees=employees,
        departments=all_departments,
        positions=all_positions,
        sel_department=department,
        sel_position=position,
        sel_q=q,
        now_year=datetime.now().year,
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
    return render_template("catalogs.html", positions=positions, departments=departments)


@app.route("/catalog/<kind>/add", methods=["POST"])
@login_required
def catalog_add(kind):
    if kind not in ("position", "department"):
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Название не может быть пустым", "error")
    else:
        db = get_db()
        try:
            db.execute(f"INSERT INTO {kind}s (name) VALUES (?)", (name,))
            db.commit()
            flash(f"Добавлено: {name}", "ok")
        except sqlite3.IntegrityError:
            flash(f"«{name}» уже существует", "error")
    return redirect(url_for("catalogs"))


@app.route("/catalog/<kind>/<int:cid>/rename", methods=["POST"])
@login_required
def catalog_rename(kind, cid):
    if kind not in ("position", "department"):
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
    if kind not in ("position", "department"):
        abort(404)
    db = get_db()
    # SET NULL снимет ссылку с сотрудников
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
    return render_template("employee_form.html", emp={}, title="Новый сотрудник",
                           positions=positions, departments=departments)


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
    return render_template("employee_form.html", emp=emp, title="Редактирование",
                           positions=positions, departments=departments)


def _clean_int(val):
    """'' -> None; остальное -> int или None."""
    try:
        v = int(val)
        return v
    except (TypeError, ValueError):
        return None


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
    db.commit()
    flash("Сотрудник сохранён", "ok")
    return redirect(url_for("employee_view", eid=eid))


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

    records = db.execute(
        "SELECT * FROM year_records WHERE employee_id=? AND year=?",
        (eid, year),
    ).fetchall()
    records = {r["semester"]: r for r in records}

    meetings = db.execute(
        "SELECT * FROM meetings WHERE employee_id=? ORDER BY date DESC, id DESC",
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
        "H", parent=base, fontName=fn if fn == "DejaVuSans" else "Helvetica-Bold",
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
    story.append(Table(
        [["Сотрудник", item["name"]], ["Отдел", item["department"]],
         ["Должность", item["position"]], ["Зарплата", item["salary"]],
         ["Дата приёма", _fmt_date(item["hire_date"])]],
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