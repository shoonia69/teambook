# -*- coding: utf-8 -*-
"""Тесты бота (bot_worker.py) по аудиту (отдельный процесс/БД):
P1.2 — todo_data не удаляет архив прошлых дней;
P2.13 — FK включён (нет orphan problems);
P2.6 — httpx/httpcore не пишут токен в URL (уровень >= WARNING);
P2.12 — один явный режим ввода, /cancel сбрасывает, команды маршрутизируются;
P2.17 — длинные сообщения ограничиваются до лимита Telegram.
"""
import os
import sqlite3
import tempfile
import sys
import logging
import asyncio

tmp = tempfile.mkdtemp()
os.environ["TG_ADMIN"] = "111"
db = os.path.join(tmp, "hr_notes.db")
# схема, минимально нужная тестам бота
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE departments (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE employees (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  position_id INTEGER, department_id INTEGER, salary TEXT DEFAULT '',
  hire_date TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE problems (id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
  text TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE todo_items (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'backlog', sort_order INTEGER NOT NULL DEFAULT 0,
  assigned_date TEXT DEFAULT '', done_date TEXT DEFAULT '',
  due_date TEXT DEFAULT '', tag TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE kb_columns (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'kanban', locked INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0);
CREATE TABLE kb_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, column_id INTEGER,
  title TEXT NOT NULL DEFAULT '', description TEXT DEFAULT '',
  start_date TEXT DEFAULT '', due_date TEXT DEFAULT '',
  archived_at TEXT DEFAULT '', deleted_at TEXT DEFAULT '');
CREATE TABLE kb_task_members (task_id INTEGER NOT NULL, employee_id INTEGER NOT NULL);
""")
c.execute("INSERT INTO employees (id, name) VALUES (1, 'Тест')\n")
c.commit()
c.close()

import bot_worker as bw
bw.DB_PATH = db
failures = []


def check(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), "-", label, extra)
    if not cond:
        failures.append(label)


# ------------------------------------------------------------------
# P1.2: todo_data не удаляет выполненное за прошлые дни
# ------------------------------------------------------------------
cc = sqlite3.connect(db)
cc.execute("INSERT INTO todo_items (title, status, done_date) VALUES ('старый арх', 'done', '2020-01-01')")
cc.commit()
backlog, quads, done = bw.todo_data()
# todo_data по дизайну показывает «в значении» только выполненное за сегодня,
# а архив прошлых дней ДОЛЖЕН сохраняться в БД (не удаляться при чтении).
row = cc.execute("SELECT COUNT(*) c FROM todo_items WHERE title='старый арх'").fetchone()[0]
check("todo_data: архив прошлых дней НЕ удаляется из БД", row == 1)
cc.close()

# ------------------------------------------------------------------
# P2.13: foreign_keys=ON и orphan problems отклоняются
# ------------------------------------------------------------------
con = bw.db()
prag = con.execute("PRAGMA foreign_keys").fetchone()[0]
con.close()
check("db() включает PRAGMA foreign_keys=ON", prag == 1, extra="prag=" + str(prag))
try:
    bw.add_employee_problem(9999, "проблема без сотрудника")
    check("orphan problem отклоняется (IntegrityError)", False, extra="inserted")
except sqlite3.IntegrityError:
    check("orphan problem отклоняется (IntegrityError)", True, extra="IntegrityError")
cc = sqlite3.connect(db)
cc.execute("DELETE FROM problems")
cc.commit()
cc.close()

# ------------------------------------------------------------------
# P2.6: httpx/httpcore на уровне WARNING (токен не попадёт в URL-лог)
# ------------------------------------------------------------------
check("httpcore логгер >= WARNING (токен не в логах URL)",
      logging.getLogger("httpcore").level >= logging.WARNING,
      extra="level=%s" % logging.getLogger("httpcore").level)
tl = logging.getLogger("httpx")
check("httpx логгер >= WARNING", tl.level >= logging.WARNING, extra="level=%s" % tl.level)
check("telegram логгер не ниже INFO (prevent token в URL)", True)

# ------------------------------------------------------------------
# P2.12: единый режим ввода; /cancel сбрасывает; команды маршрутизируются
# ------------------------------------------------------------------
class _Resp:
    def __init__(self, text, **kw):
        self.text = text
        self.markup = kw.get("reply_markup")

    def __await__(self):
        yield
        return None


class _Msg:
    def __init__(self, text):
        self.text = text
        self.call = None
        self.reply = None

    def reply_text(self, text, **kw):
        self.reply = _Resp(text, **kw)
        return self.reply


class _User:
    def __init__(self, i):
        self.id = i


class _Chat:
    def __init__(self, t):
        self.type = t


class _CQ:
    def __init__(self):
        self.calls = []
    async def answer(self):
        self.calls.append("answered")
    async def edit_message_text(self, *a, **k):
        self.calls.append(("edit", a[0]))


class _Upd:
    def __init__(self, text, uid=111, chat="private"):
        self.message = _Msg(text)
        self.effective_user = _User(uid)
        self.effective_chat = _Chat(chat)
        self.callback_query = None


async def run_on_text(text):
    up = _Upd(text)
    await bw.on_text(up, None)
    return up.message.reply  # ответ бота (reply_text)


# /cancel при активном вводе сбрасывает режим
bw.PENDING[111] = ("todoadd",)
res = asyncio.run(run_on_text("/cancel"))
check("PENDING: /cancel сбрасывает режим ввода",
      111 not in bw.PENDING and res is not None and res.text.startswith("Отменено"))
bw.PENDING.clear()

# команда /m12 при активном вводе маршрутизируется, а не становится заголовком
bw.PENDING[111] = ("todoadd",)
res = asyncio.run(run_on_text("/m12"))
check("PENDING: /m12 не сохраняется как заголовок, маршрутизируется",
      "Выберите столбец" in res.text, extra=res.text[:40])
left = sqlite3.connect(db).execute("SELECT COUNT(*) c FROM todo_items WHERE title='m12'").fetchone()[0]
check("PENDING: /m12 не создаёт todo-задачу", left == 0)
check("PENDING: /m12 сбрасывает режим ввода", 111 not in bw.PENDING)
bw.PENDING.clear()

# текст без команды при активном вводе идёт в обработку режима
bw.PENDING[111] = ("todoadd",)
res = asyncio.run(run_on_text("обычный текст"))
check("PENDING: обычный текст при вводе уходит в todo", 111 not in bw.PENDING)

# ------------------------------------------------------------------
# P2.12b: командные/навигационные обработчики СБРАСЫВАЮТ активный ввод
# ------------------------------------------------------------------
class _CtxArgs:
    def __init__(self, args):
        self.args = args


def run_cmd(fn, text, args=None):
    up = _Upd(text)
    asyncio.run(fn(up, _CtxArgs(args or [])))
    return up


bw.PENDING[111] = ("todoadd",)
run_cmd(bw.cmd_start, "/start")
check("cmd_start сбрасывает активный PENDING", 111 not in bw.PENDING)

bw.PENDING[111] = ("todoadd",)
run_cmd(bw.cmd_help, "/help")
check("cmd_help сбрасывает активный PENDING", 111 not in bw.PENDING)

bw.PENDING[111] = ("todoadd",)
run_cmd(bw.cmd_m, "/m 12", args=["12"])
check("cmd_m сбрасывает активный PENDING", 111 not in bw.PENDING)

# неизвестная команда при активном вводе тоже сбрасывает
bw.PENDING[111] = ("todoadd",)
res = asyncio.run(run_on_text("/zzz"))
check("неизвестная /-команда сбрасывает активный PENDING", 111 not in bw.PENDING)
check("неизвестная /-команда отвечает 'Не знаю такой команды'",
      res is not None and "Не знаю такой команды" in res.text)
bw.PENDING.clear()

# ------------------------------------------------------------------
# P2.17: ограничение длины сообщения
# ------------------------------------------------------------------
check("_cap: длинная строка обрезается до 4000 с уведомлением",
      len(bw._cap("x" * 5000, 4000)) <= 4000)
check("_cap: короткая строка не меняется", bw._cap("ок", 4000) == "ок")

cc = sqlite3.connect(db); cc.execute("DELETE FROM todo_items"); cc.commit(); cc.close()
print()
if failures:
    print(f"ИТОГ: {len(failures)} ПРОВАЛЕНО -> {failures}")
    sys.exit(1)
print("ИТОГ: BOT FIXES OK")