# -*- coding: utf-8 -*-
"""
Telegram-бот TeamBook — дублирует функции сайта (сотрудники, записи,
проблемы, канбан). Запускается супервизором bot.py, который передаёт
токен и ID администратора через переменные окружения.
"""
import os
import sqlite3
import logging
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("teambook-bot")

TOKEN = os.environ.get("TG_TOKEN", "")
ADMIN = int(os.environ.get("TG_ADMIN", "0") or 0)
DB_PATH = os.environ.get("TG_DB", "/app/data/hr_notes.db")

# Состояния незавершённого ввода (ожидание текста)
PENDING = {}


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# --------------------------------------------------------------------------- #
# Хелперы авторизации и форматирования
# --------------------------------------------------------------------------- #
def is_admin(uid):
    return bool(ADMIN) and uid == ADMIN


def esc(s):
    """Экранирование для MarkdownV2."""
    if s is None:
        s = ""
    import re
    return re.sub(r'([_*[\]()~`>#+\-=|{}.!])', r"\\\1", str(s))


def _fmt_date(v):
    if not v:
        return ""
    try:
        return datetime.strptime(v[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return v[:10]


# --------------------------------------------------------------------------- #
# Данные из БД
# --------------------------------------------------------------------------- #
def list_employees():
    c = db()
    rows = c.execute(
        """SELECT e.id, e.name, e.salary, e.hire_date,
                  p.name AS position, d.name AS department
           FROM employees e
           LEFT JOIN positions p ON p.id = e.position_id
           LEFT JOIN departments d ON d.id = e.department_id
           WHERE e.active = 1 ORDER BY e.name COLLATE NOCASE""").fetchall()
    c.close()
    return rows


def find_employee(q):
    c = db()
    ql = q.lower()
    rows = c.execute(
        """SELECT e.id, e.name, e.salary, e.hire_date,
                  p.name AS position, d.name AS department
           FROM employees e
           LEFT JOIN positions p ON p.id = e.position_id
           LEFT JOIN departments d ON d.id = e.department_id
           WHERE e.active = 1 ORDER BY e.name COLLATE NOCASE""").fetchall()
    c.close()
    return [r for r in rows if ql in r["name"].lower()]


def employee_records(eid):
    c = db()
    rows = c.execute(
        "SELECT * FROM year_records WHERE employee_id=? ORDER BY year DESC, semester",
        (eid,)).fetchall()
    c.close()
    return rows


def list_problems():
    c = db()
    rows = c.execute(
        """SELECT p.id, p.text, p.created_at, e.name AS emp, e.id AS eid
           FROM problems p JOIN employees e ON e.id = p.employee_id
           ORDER BY p.id DESC""").fetchall()
    c.close()
    return rows


def problems_for(eid):
    c = db()
    rows = c.execute(
        """SELECT p.id, p.text, p.created_at
           FROM problems p WHERE p.employee_id=?
           ORDER BY p.id DESC""", (eid,)).fetchall()
    c.close()
    return rows


def board_data():
    """Возвращает (columns, tasks, members)."""
    c = db()
    columns = c.execute(
        "SELECT * FROM kb_columns WHERE kind='kanban' ORDER BY sort_order, id").fetchall()
    tasks = c.execute(
        """SELECT * FROM kb_tasks
           WHERE archived_at='' AND deleted_at=''
           ORDER BY id DESC""").fetchall()
    mem = c.execute(
        """SELECT m.task_id, e.name
           FROM kb_task_members m JOIN employees e ON e.id = m.employee_id
           ORDER BY e.name""").fetchall()
    c.close()
    members = {}
    for r in mem:
        members.setdefault(r["task_id"], []).append(r["name"])
    col_by_id = {x["id"]: x for x in columns}
    return columns, tasks, members, col_by_id


def all_columns():
    c = db()
    rows = c.execute(
        "SELECT * FROM kb_columns WHERE kind='kanban' ORDER BY sort_order, id").fetchall()
    c.close()
    return rows


def add_employee_problem(eid, text):
    c = db()
    c.execute("INSERT INTO problems (employee_id, text) VALUES (?,?)", (eid, text))
    c.commit()
    c.close()


def delete_problem(pid):
    c = db()
    c.execute("DELETE FROM problems WHERE id=?", (pid,))
    c.commit()
    c.close()


def add_task(title, column_id, desc=""):
    c = db()
    cur = c.execute(
        "INSERT INTO kb_tasks (title, column_id, description) VALUES (?,?,?)",
        (title, column_id or None, desc))
    c.commit()
    tid = cur.lastrowid
    c.close()
    return tid


def task_by_id(tid):
    c = db()
    row = c.execute("SELECT * FROM kb_tasks WHERE id=? AND deleted_at=''", (tid,)).fetchone()
    c.close()
    return row


def move_task(tid, column_id):
    c = db()
    c.execute("UPDATE kb_tasks SET column_id=? WHERE id=?", (column_id, tid))
    c.commit()
    c.close()


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #
def main_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👥 Сотрудники", callback_data="emp"),
        InlineKeyboardButton("🛠 Проблемы", callback_data="probs"),
    ], [
        InlineKeyboardButton("📋 Канбан-доска", callback_data="board"),
        InlineKeyboardButton("❓ Помощь", callback_data="help"),
    ]])


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён. Бот настроен для одного администратора.")
        return
    await update.message.reply_text(
        "👋 TeamBook в Telegram\n\nДублирует функции сайта — сотрудники, "
        "записи, проблемы и канбан-доска.\nВыберите раздел:",
        reply_markup=main_kb())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "Команды:\n/start — главное меню\n"
        "/help — эта справка\n\n"
        "В меню доступны сотрудники (с записями), проблемы и канбан-доска.")


# --------------------------------------------------------------------------- #
# Основное меню и навигация
# --------------------------------------------------------------------------- #
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    data = q.data

    if data == "help":
        await q.edit_message_text(
            "Разделы:\n👥 Сотрудники — карточки, записи по полугодиям, проблемы\n"
            "🛠 Проблемы — общий список и добавление\n"
            "📋 Канбан — доска с задачами и перенос",
            reply_markup=main_kb())
        return

    if data == "emp":
        await show_employees(q)
        return
    if data.startswith("emp:"):
        await show_employee_detail(q, int(data.split(":")[1]))
        return

    if data == "probs":
        await show_problems(q, 0)
        return
    if data.startswith("probs_pg:"):
        await show_problems(q, int(data.split(":")[1]))
        return
    if data == "probs_add":
        await q.edit_message_text("Выберите сотрудника:", reply_markup=emp_pick_kb("probad"))
        return
    if data.startswith("probad:"):
        PENDING[f"probadd:{update.effective_user.id}"] = int(data.split(":")[1])
        await q.edit_message_text(
            "✍️ Введите текст проблемы сотруднику (или /cancel):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="probs")]]))
        return
    if data.startswith("probd:"):
        delete_problem(int(data.split(":")[1]))
        await show_problems(q, 0)
        return

    if data == "board":
        await show_board(q)
        return
    if data == "board_new":
        PENDING[f"newtask:{update.effective_user.id}"] = True
        await q.edit_message_text(
            "✍️ Введите название новой задачи (упадёт в 📥 Бэклог).\n"
            "Можно добавить описание после « | »: <название> | <описание>\n(/cancel для отмены)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="board")]]))
        return
    if data.startswith("task_move:"):
        await show_move_targets(q, int(data.split(":")[1]))
        return
    if data.startswith("mv:"):
        # mv:TASK:COL
        _, tid, cid = data.split(":")
        move_task(int(tid), int(cid))
        await show_board(q)
        return


# сотрудники
async def show_employees(q):
    emps = list_employees()
    kbd = []
    for e in emps:
        kbd.append([InlineKeyboardButton(f"👤 {e['name']}", callback_data=f"emp:{e['id']}")])
    kbd.append([InlineKeyboardButton("← Главное меню", callback_data="help")])
    await q.edit_message_text(
        f"👥 Сотрудники ({len(emps)}):\nНажмите для карточки.",
        reply_markup=InlineKeyboardMarkup(kbd))


def emp_pick_kb(prefix):
    emps = list_employees()
    kbd = []
    for e in emps:
        kbd.append([InlineKeyboardButton(e["name"], callback_data=f"{prefix}:{e['id']}")])
    kbd.append([InlineKeyboardButton("← Назад", callback_data="probs")])
    return InlineKeyboardMarkup(kbd)


async def show_employee_detail(q, eid):
    emps = list_employees()
    e = next((r for r in emps if r["id"] == eid), None)
    if not e:
        await q.edit_message_text("Сотрудник не найден.", reply_markup=main_kb())
        return
    recs = employee_records(eid)
    probs = problems_for(eid)
    lines = [
        f"👤 {e['name']}",
        f"🏢 Отдел: {e['department'] or '—'}",
        f"💼 Должность: {e['position'] or '—'}",
        f"💰 Зарплата: {e['salary'] or '—'}",
        f"📅 Приём: {_fmt_date(e['hire_date']) or '—'}",
    ]
    if recs:
        lines.append("\n📝 Записи по полугодиям:")
        for r in recs[:6]:
            sem = "1П" if r["semester"] == "1H" else "2П"
            lines.append(f"• {r['year']} ({sem}): {r['goals_employee'] or r['comments'] or '…'}")
    if probs:
        lines.append(f"\n⚠️ Проблемы ({len(probs)}):")
        for p in probs:
            lines.append(f"• {p['text'][:60]}")
    kbd = [[
        InlineKeyboardButton("← Сотрудники", callback_data="emp"),
        InlineKeyboardButton("Главное меню", callback_data="help"),
    ]]
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kbd))


# проблемы
async def show_problems(q, page):
    probs = list_problems()
    if not probs:
        kbd = [
            [InlineKeyboardButton("➕ Добавить", callback_data="probs_add")],
            [InlineKeyboardButton("← Главное меню", callback_data="help")],
        ]
        await q.edit_message_text("🛠 Проблем пока нет.", reply_markup=InlineKeyboardMarkup(kbd))
        return
    per = 8
    plist = probs[page * per:(page + 1) * per]
    lines = [f"🛠 Проблемы ({len(probs)}):"]
    kbd = []
    for p in plist:
        kbd.append([InlineKeyboardButton(
            f"{p['emp']}: {p['text'][:40]}",
            callback_data=f"probd:{p['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("←", callback_data=f"probs_pg:{page-1}"))
    nav.append(InlineKeyboardButton("➕", callback_data="probs_add"))
    if (page + 1) * per < len(probs):
        nav.append(InlineKeyboardButton("→", callback_data=f"probs_pg:{page+1}"))
    kbd.append(nav)
    kbd.append([InlineKeyboardButton("← Главное меню", callback_data="help")])
    await q.edit_message_text(
        "\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup(kbd))


# канбан
async def show_board(q):
    columns, tasks, members, col_by_id = board_data()
    lines = ["📋 Канбан-доска\n"]
    for col in columns:
        ct = [t for t in tasks if t["column_id"] == col["id"]]
        lines.append(f"▫️ {col['name']} ({len(ct)})")
        for t in ct[:8]:
            who = ", ".join(members.get(t["id"], []))
            w = f" · 👤 {who}" if who else ""
            lines.append(f"   • {t['title']} [{t['id']}]{w}")
        if not ct:
            lines.append("   (пусто)")
        lines.append("")
    if not columns:
        lines.append("Столбцов нет.")
    kbd = [[
        InlineKeyboardButton("➕ Новая задача", callback_data="board_new"),
        InlineKeyboardButton("← Главное меню", callback_data="help"),
    ]]
    await q.edit_message_text("\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup(kbd))


async def show_move_targets(q, tid):
    t = task_by_id(tid)
    if not t:
        await q.edit_message_text("Задача не найдена.", reply_markup=main_kb())
        return
    cols = all_columns()
    kbd = []
    for cid in cols:
        mark = "✔ " if cid["id"] == t["column_id"] else ""
        kbd.append([InlineKeyboardButton(f"{mark}{cid['name']}", callback_data=f"mv:{tid}:{cid['id']}")])
    kbd.append([InlineKeyboardButton("← Доска", callback_data="board")])
    await q.edit_message_text(
        f"Переместить «{t['title']}»\nВыберите столбец:", reply_markup=InlineKeyboardMarkup(kbd))


# --------------------------------------------------------------------------- #
# Текстовые сообщения (перехват ожидаемого ввода)
# --------------------------------------------------------------------------- #
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    uid = update.effective_user.id
    text = update.message.text.strip()

    if text == "/cancel":
        PENDING.pop(f"probadd:{uid}", None)
        PENDING.pop(f"newtask:{uid}", None)
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return

    if f"probadd:{uid}" in PENDING:
        eid = PENDING.pop(f"probadd:{uid}")
        add_employee_problem(eid, text)
        await update.message.reply_text("✅ Проблема добавлена.", reply_markup=main_kb())
        return

    if f"newtask:{uid}" in PENDING:
        PENDING.pop(f"newtask:{uid}")
        title, _, desc = text.partition("|")
        columns = all_columns()
        backlog = next((c for c in columns if c["locked"] == 1), None)
        tid = add_task(title.strip(), backlog["id"] if backlog else None, desc.strip())
        await update.message.reply_text(f"✅ Задача #{tid} добавлена в 📥 Бэклог.",
                                        reply_markup=main_kb())
        return

    # попытка переместить: /m12
    if text.startswith("/m"):
        tid = text[2:].strip()
        if tid.isdigit():
            await update.message.reply_text(
                "Выберите столбец:", reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(c["name"], callback_data=f"mv:{int(tid)}:{c['id']}")]
                     for c in all_columns()] + [[InlineKeyboardButton("← Отмена", callback_data="board")]]))
            return

    # поиск сотрудника
    emps = find_employee(text)
    if emps:
        kbd = [[InlineKeyboardButton(e["name"], callback_data=f"emp:{e['id']}")] for e in emps]
        kbd.append([InlineKeyboardButton("← Главное меню", callback_data="help")])
        await update.message.reply_text(
            f"Найдено сотрудников: {len(emps)}",
            reply_markup=InlineKeyboardMarkup(kbd))
        return

    await update.message.reply_text(
        "Не понял команду. Используйте /start или /m<номер> для переноса задачи.",
        reply_markup=main_kb())


async def cmd_m(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    tid = int(context.args[0]) if context.args and context.args[0].isdigit() else None
    if not tid:
        await update.message.reply_text("Формат: /m <номер задачи>")
        return
    cols = all_columns()
    kbd = [[InlineKeyboardButton(c["name"], callback_data=f"mv:{tid}:{c['id']}")]
           for c in cols] + [[InlineKeyboardButton("← Отмена", callback_data="board")]]
    await update.message.reply_text(f"Переместить задачу #{tid}:", reply_markup=InlineKeyboardMarkup(kbd))


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #
def main():
    if not TOKEN:
        log.error("TG_TOKEN не задан — воркер не запущен")
        return
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("m", cmd_m))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT, on_text))
    log.info("Воркер запущен (admin=%s)", ADMIN)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()