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
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, redirect, url_for, render_template, session, flash, abort, g,
)

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
    years = [r["year"] for r in db.execute(
        "SELECT DISTINCT year FROM year_records ORDER BY year DESC").fetchall()]

    all_years = list(years)
    if not all_years:
        all_years = [datetime.now().year]

    year = request.args.get("year", type=int, default=None)
    if year is None:
        year = all_years[0] if all_years else datetime.now().year

    # Фильтры по отделу и должности
    department = request.args.get("department", "").strip()
    position = request.args.get("position", "").strip()

    all_departments = [r["name"] for r in db.execute(
        "SELECT DISTINCT d.name FROM departments d "
        "JOIN employees e ON e.department_id = d.id WHERE e.active = 1 "
        "ORDER BY d.name").fetchall()]
    all_positions = [r["name"] for r in db.execute(
        "SELECT DISTINCT p.name FROM positions p "
        "JOIN employees e ON e.position_id = p.id WHERE e.active = 1 "
        "ORDER BY p.name").fetchall()]

    where = ["e.active = 1"]
    params = [year]
    if department:
        where.append("d.name = ?")
        params.append(department)
    if position:
        where.append("p.name = ?")
        params.append(position)

    rows = db.execute(
        f"""
        SELECT e.*, p.name AS position, d.name AS department,
               yr.semester, yr.goals_employee, yr.proposals_manager,
               yr.wishes_employee, yr.comments, yr.updated_at
        FROM employees e
        LEFT JOIN positions p ON p.id = e.position_id
        LEFT JOIN departments d ON d.id = e.department_id
        LEFT JOIN year_records yr
               ON yr.employee_id = e.id AND yr.year = ?
        WHERE {' AND '.join(where)}
        ORDER BY d.name, e.name
        """,
        tuple(params),
    ).fetchall()

    employees = {}
    for r in rows:
        if r["id"] not in employees:
            employees[r["id"]] = {
                "id": r["id"], "name": r["name"], "position": r["position"],
                "department": r["department"], "salary": r["salary"],
                "records": {},
            }
        if r["semester"]:
            employees[r["id"]]["records"][r["semester"]] = {
                "goals_employee": r["goals_employee"],
                "proposals_manager": r["proposals_manager"],
                "wishes_employee": r["wishes_employee"],
                "comments": r["comments"],
                "updated_at": r["updated_at"],
            }

    return render_template(
        "index.html",
        employees=list(employees.values()),
        years=all_years,
        year=year,
        semesters=SEMESTERS,
        departments=all_departments,
        positions=all_positions,
        sel_department=department,
        sel_position=position,
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
    if year is None:
        year = years[0]["year"] if years else datetime.now().year

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
            proposals_manager, wishes_employee, comments, updated_at)
        VALUES (?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(employee_id, year, semester) DO UPDATE SET
            goals_employee=excluded.goals_employee,
            proposals_manager=excluded.proposals_manager,
            wishes_employee=excluded.wishes_employee,
            comments=excluded.comments,
            updated_at=datetime('now')
        """,
        (
            eid, year, semester,
            request.form.get("goals_employee", ""),
            request.form.get("proposals_manager", ""),
            request.form.get("wishes_employee", ""),
            request.form.get("comments", ""),
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