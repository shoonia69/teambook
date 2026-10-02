# -*- coding: utf-8 -*-
"""Регрессионные тесты пакета 2 (UX/доступность/асинхрон/хоткеи).

Проверяют на рендеренном HTML/JS, что в базовый шаблон и страницы
доски/todo внедрены обязательные артефакты пакета 2:
  1. Доступные модалки (role=dialog, aria-modal, фокус-ловушка, dirty-state);
  2. Единый fetch-POST с проверкой response.ok (checkOk) и тостом/статусом;
  3. Безопасные хоткеи (игнор полей/composition/модификаторов);
  4. Общие классы форм/фильтров (page-head, compact-filter, form-actions);
  5. Адаптив 320–390px (media max-width:420px) без переполнения.
"""
import os
import tempfile
import re
import sys

tmp = tempfile.mkdtemp()
os.environ["HR_DATA_DIR"] = tmp
os.environ["HR_PASSWORD"] = "x"

import app as appmod

appmod.init_db()

# наполнить минимум данных, чтобы страницы рендерили таблицы и модалки
with appmod.app.app_context():
    db = appmod.get_db()
    cur = db.execute("INSERT INTO positions(name) VALUES ('Инженер')")
    pid = cur.lastrowid
    cur = db.execute("INSERT INTO departments(name) VALUES ('ТП')")
    did = cur.lastrowid
    db.execute("INSERT INTO employees(name,position_id,department_id) VALUES ('Иванов',?,?)", (pid, did))
    cur = db.execute("INSERT INTO kb_columns(name,kind,sort_order) VALUES ('Бэклог','kanban',1)")
    cid = cur.lastrowid
    cur = db.execute("INSERT INTO kb_tasks(title,column_id) VALUES ('Задача',?)", (cid,))
    tid = cur.lastrowid
    cur = db.execute("INSERT INTO todo_items(title,status) VALUES ('Тодо','backlog')")
    tdid = cur.lastrowid
    db.commit()

c = appmod.app.test_client()
c.post("/login", data={"password": "x"})

fail = []


def has(text, needle, label):
    ok = needle in text
    if not ok:
        fail.append(f"{label}: отсутствует {needle!r}")
    return ok


head = c.get("/todo").get_data(as_text=True)          # base.html скрипт
with open(os.path.join(os.path.dirname(__file__), "static", "style.css"), encoding="utf-8") as f:
    base_css = f.read()

board = c.get("/board").get_data(as_text=True)
todo = c.get("/todo").get_data(as_text=True)
index = c.get("/").get_data(as_text=True)

# ---- 1. Доступные модалки ----
has(head, "window.initModal", "модальный менеджер в base")
has(head, "setAttribute('role', 'dialog')", "role=dialog в initModal")
has(head, "setAttribute('aria-modal', 'true')", "aria-modal в initModal")
has(head, "function trap(e)", "фокус-ловушка Tab в initModal")
has(head, "overlay.style.display === 'flex'", "Escape только при открытой модалке")
has(head, "Есть несохранённые изменения", "dirty-state при закрытии")
has(head, "lastTrigger.focus()", "возврат фокуса на триггер")
has(head, "!overlay.contains(document.activeElement)", "focus trap возвращает внешний фокус")
has(head, "el.inert = true", "фон становится inert при открытии модалки")
has(todo, "todoModal", "модалка todo идёт через initModal")
has(todo, "todoModal.open(returnFocus)", "todo возвращает фокус на исходную кнопку")
has(board, 'role="button" tabindex="0"', "карточки доски доступны с клавиатуры")
has(board, "openCardKey(event", "карточки доски открываются Enter/Space")
has(board, "cardModal.open(returnFocus)", "фокус возвращается на исходную карточку")

# ---- 2. Асинхронный fetch: response.ok, pending, тост/alert ----
has(head, "if (opts.checkOk && !r.ok) return Promise.reject", "проверка response.ok")
has(head, "if (opts.onError) opts.onError(err)", "ошибка fetch обработана без unhandled rejection")
has(head, "aria-busy", "pending-состояние (aria-busy)")
has(head, "window.tbToast", "тост-хелпер")
has(head, "setAttribute('role', isErr ? 'alert' : 'status')", "alert/status-роль тоста")
has(head, "id=\"tb-toast\"", "контейнер тоста в base")
# готовые хоткеи/ошибки на доске и todo не должны рвать страницу — модалки закрыты через менеджер
has(board, "window.postForm", "доска использует postForm")
has(todo, "window.postForm", "todo использует postForm")

# ---- 3. Безопасные хоткеи ----
has(head, "window.hotkeyGuard", "hotkeyGuard в base")
has(head, "ctrlKey || e.altKey || e.metaKey", "игнор модификаторов")
has(head, "isComposing", "игнор IME-composition")
has(head, "isEditableTarget", "игнор редактируемых полей")
has(todo, "window.hotkeyGuard(e)", "хоткей todo под guard")

# ---- 4. Общие классы форм/фильтров ----
has(base_css, ".page-head", "класс page-head в css")
has(base_css, ".compact-filter", "класс compact-filter в css")
has(base_css, ".form-actions", "класс form-actions в css")
has(index, "class=\"compact-filter\"", "index использует compact-filter (одна строка фильтров)")
has(index, "name=\"department\"", "фильтр отдела в одной форме")
has(index, "name=\"position\"", "фильтр должности в одной форме")

# ---- 5. Адаптив 320–390px ----
has(base_css, "@media (max-width: 420px)", "media-запрос 420px")
has(base_css, ".modal-card { max-width: 100%", "модалка без переполнения на узких")

print()
if fail:
    print("ИТОГ: ПРОВАЛЫ ->")
    for f_ in fail:
        print("  -", f_)
    sys.exit(1)
print("ИТОГ: UX PACK 2 OK")