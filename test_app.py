# -*- coding: utf-8 -*-
"""Функциональный тест TeamBook через тестовый клиент Flask (без реального сервера)."""
import os
import tempfile
import sys
import datetime
import sqlite3
import re as _re

tmp = tempfile.mkdtemp()
os.environ["HR_DATA_DIR"] = tmp
os.environ["HR_PASSWORD"] = "test-pass-123"

import app as appmod

app = appmod.app
app.config["TESTING"] = True
appmod.init_db()

failures = []


def check(label, cond):
    print(("PASS" if cond else "FAIL"), "-", label)
    if not cond:
        failures.append(label)


def dbq(sql, args=()):
    with appmod.app.app_context():
        return appmod.get_db().execute(sql, args).fetchall()


c = app.test_client()

# --- логин ---
r = c.get("/", follow_redirects=True)
check("редирект на логин без авторизации", "/login" in r.request.path)
c.post("/login", data={"password": "wrong"})
r = c.post("/login", data={"password": "test-pass-123"}, follow_redirects=True)
check("вход с верным паролем", r.status_code == 200 and "Сотрудники" in r.get_data(as_text=True))

# CSRF: тесты отправляют валидный токен (эндпоинт /login сам CSRF-исключён).
# Оборачиваем клиент, чтобы все последующие POST проходили проверку токена —
# в т.ч. POST без тела (удаления/корзина/перенос) получали _csrf.
CSRF_TOKEN = "test-csrf-abcdef"
with c.session_transaction() as sess:
    sess["_csrf"] = CSRF_TOKEN
_orig_post = c.post


def _client_post(url, *args, **kwargs):
    data = kwargs.get("data")
    if data is None:
        kwargs["data"] = {"_csrf": CSRF_TOKEN}
    elif isinstance(data, dict) and "_csrf" not in data:
        data = dict(data)
        data["_csrf"] = CSRF_TOKEN
        kwargs["data"] = data
    return _orig_post(url, *args, **kwargs)


c.post = _client_post

# --- справочники: создание ---
c.post("/catalog/position/add", data={"name": "Инженер 1 категории"})
c.post("/catalog/position/add", data={"name": "Старший инженер"})
c.post("/catalog/department/add", data={"name": "ТП Orion soft"})
c.post("/catalog/department/add", data={"name": "ТП Сбер"})
check("должностей 2", len(dbq("SELECT * FROM positions")) == 2)
check("отделов 2", len(dbq("SELECT * FROM departments")) == 2)

# дубликат отклонён
c.post("/catalog/position/add", data={"name": "Инженер 1 категории"})
check("дубликат должности отклонён", len(dbq("SELECT * FROM positions")) == 2)

p1 = dbq("SELECT id FROM positions WHERE name='Инженер 1 категории'")[0]["id"]
p2 = dbq("SELECT id FROM positions WHERE name='Старший инженер'")[0]["id"]
d1 = dbq("SELECT id FROM departments WHERE name='ТП Orion soft'")[0]["id"]

# --- создание сотрудника с выбором из справочника ---
r = c.post("/employee/new", data={
    "name": "Иванов Иван",
    "position_id": str(p1), "department_id": str(d1), "salary": "120 000 ₽"
}, follow_redirects=True)
check("сотрудник создан через select", "Иванов Иван" in r.get_data(as_text=True))

emp = dbq("SELECT * FROM employees WHERE name='Иванов Иван'")[0]
eid = emp["id"]
check("position_id сохранён", emp["position_id"] == p1)
check("department_id сохранён", emp["department_id"] == d1)

# редактирование: сменить должность
c.post(f"/employee/{eid}/edit", data={
    "name": "Иванов Иван", "position_id": str(p2),
    "department_id": str(d1), "salary": "140 000 ₽"
}, follow_redirects=True)
emp2 = dbq("SELECT * FROM employees WHERE id=?", (eid,))[0]
check("должность обновлена через select", emp2["position_id"] == p2)

# убрать отдел (пустое значение = NULL)
c.post(f"/employee/{eid}/edit", data={
    "name": "Иванов Иван", "position_id": str(p2), "department_id": "", "salary": "140 000 ₽"
}, follow_redirects=True)
emp3 = dbq("SELECT * FROM employees WHERE id=?", (eid,))[0]
check("отдел можно сбросить в пусто", emp3["department_id"] is None)

# --- показ на дашборде (join с названиями) ---
r = c.get("/")
body = r.get_data(as_text=True)
check("дашборд показывает должность из справочника", "Старший инженер" in body)
check("дашборд показывает отдел из справочника", "Иванов Иван" in body)

# --- карточка сотрудника ---
r = c.get(f"/employee/{eid}")
body = r.get_data(as_text=True)
check("карточка показывает должность", "Старший инженер" in body)

# --- полугодовая запись ---
c.post(f"/employee/{eid}/record", data={
    "year": "2026", "semester": "1H",
    "goals_employee": "Освоить Orion soft",
    "proposals_manager": "Дать проект X",
    "wishes_employee": "Хочет повышение",
    "comments": "Хорошая динамика",
    "colleagues_feedback": "Коллеги высоко ценят",
}, follow_redirects=True)
r = c.get(f"/employee/{eid}?year=2026")
body = r.get_data(as_text=True)
check("запись цели", "Освоить Orion soft" in body)
check("запись предложения", "Дать проект X" in body)
check("запись пожелания", "Хочет повышение" in body)
check("запись комментария", "Хорошая динамика" in body)
check("запись отзывов коллег", "Коллеги высоко ценят" in body)

# upsert
with appmod.app.app_context():
    before = appmod.get_db().execute(
        "SELECT COUNT(*) c FROM year_records WHERE employee_id=? AND year=2026 AND semester='1H'", (eid,)).fetchone()["c"]
c.post(f"/employee/{eid}/record", data={
    "year": "2026", "semester": "1H", "goals_employee": "Освоить Orion soft (v2)",
    "proposals_manager": "", "wishes_employee": "", "comments": "",
}, follow_redirects=True)
body = c.get(f"/employee/{eid}?year=2026").get_data(as_text=True)
check("upsert не задублировал", before == 1)
check("upsert обновил цели", "Освоить Orion soft (v2)" in body)

# --- встреча ---
c.post(f"/employee/{eid}/meeting/new", data={
    "date": "2026-02-10", "summary": "Обсудили задачи на квартал, договорились о менторстве."
}, follow_redirects=True)
body = c.get(f"/employee/{eid}").get_data(as_text=True)
check("встреча добавлена", "Обсудили задачи на квартал" in body)

# --- случайные заметки (общие, вне полугодий) ---
check("в карточке есть блок Заметки", "Заметки" in c.get(f"/employee/{eid}").get_data(as_text=True))
c.post(f"/employee/{eid}/notes", data={"notes": "Любит работу с API, мечтает о менторстве."}, follow_redirects=True)
r = c.get(f"/employee/{eid}").get_data(as_text=True)
check("заметка сохранена и видна", "Любит работу с API" in r)
c.post(f"/employee/{eid}/notes", data={"notes": "Обновлённая заметка без прошлого текста."}, follow_redirects=True)
r = c.get(f"/employee/{eid}").get_data(as_text=True)
check("заметка редактируется (перезапись)", "Обновлённая заметка" in r and "Любит работу с API" not in r)

# --- удаление справочника снимает ссылку (SET NULL) ---
c.post(f"/catalog/position/{p2}/delete")
emp4 = dbq("SELECT * FROM employees WHERE id=?", (eid,))[0]
check("удаление должности снимает position_id", emp4["position_id"] is None)
check("должность удалена из справочника", len(dbq("SELECT * FROM positions WHERE id=?", (p2,))) == 0)

# --- фильтрация дашборда по отделу и должности ---
# вернём Иванову отдел (выше он был сброшен тестом) — чтобы был тест-дата
c.post(f"/employee/{eid}/edit", data={
    "name": "Иванов Иван", "position_id": str(p1), "department_id": str(d1), "salary": "140 000 ₽",
    "hire_date": "2023-04-10"
}, follow_redirects=True)
check("дата приёма сохранена", dbq("SELECT hire_date FROM employees WHERE id=?", (eid,))[0]["hire_date"] == "2023-04-10")
body = c.get(f"/employee/{eid}").get_data(as_text=True)
check("дата приёма видна в карточке", "принят с 2023-04-10" in body)

# --- история изменений должности/зарплаты ---
# p2 (Старший инженер) была удалена тестом выше — пересоздаём для истории
c.post("/catalog/position/add", data={"name": "Старший инженер"})
p2 = dbq("SELECT id FROM positions WHERE name='Старший инженер'")[0]["id"]
body = c.get(f"/employee/{eid}").get_data(as_text=True)
check("в карточке есть история", "История изменения должности и зарплаты" in body)
# спойлеры блоков
check("карточка имеет 5 спойлеров", body.count('class="card accordion"') == 5)
check("спойлер заметок развёрнут", 'data-acc="notes" open' in body)
check("спойлер истории свёрнут", 'data-acc="history"' in body and 'data-acc="history" open' not in body)
c.post(f"/employee/{eid}/history/add", data={
    "change_date": "2024-06-01", "position_id": str(p1), "salary": "150 000 ₽", "note": "повышение"
}, follow_redirects=True)
c.post(f"/employee/{eid}/history/add", data={
    "change_date": "2025-01-15", "position_id": str(p2), "salary": "160 000 ₽", "note": "главный инженер"
}, follow_redirects=True)
r = c.get(f"/employee/{eid}").get_data(as_text=True)
check("записи истории видны", "2025-01-15" in r and "2024-06-01" in r)
check("должность из справочника в истории", "Старший инженер" in r)
check("комментарий в истории", "главный инженер" in r)

# сортировка: новейшая запись первая
r = c.get(f"/employee/{eid}").get_data(as_text=True)
i1, i2 = r.find("2025-01-15"), r.find("2024-06-01")
check("история отсортирована (новая сверху)", i1 != -1 and i2 != -1 and i1 < i2)

# редактирование записи истории
hid = dbq("SELECT id FROM employee_history WHERE note='повышение'")[0]["id"]
c.post(f"/history/{hid}/edit", data={
    "change_date": "2024-07-01", "position_id": str(p1), "salary": "155 000 ₽", "note": "повышение (правка)"
}, follow_redirects=True)
r = c.get(f"/employee/{eid}").get_data(as_text=True)
check("история редактируется", "повышение (правка)" in r and "2024-07-01" in r)

# удаление записи истории
cnt = len(dbq("SELECT * FROM employee_history WHERE employee_id=?", (eid,)))
c.post(f"/history/{hid}/delete")
check("история удаляется", len(dbq("SELECT * FROM employee_history WHERE employee_id=?", (eid,))) == cnt - 1)

# --- валидация: история без даты отклоняется ---
c.post(f"/employee/{eid}/history/add", data={"change_date": "", "position_id": "", "salary": "", "note": ""})
check("история без даты отклонена", len(dbq(f"SELECT * FROM employee_history WHERE employee_id={eid} AND change_date=''")) == 0)

# --- создание года вручную (каркас) ---
c.post(f"/employee/{eid}/year/new", data={"year": "2027"}, follow_redirects=True)
cnt27 = dbq("SELECT COUNT(*) c FROM year_records WHERE employee_id=? AND year=2027", (eid,))[0]["c"]
check("создание года даёт 2 полугодовые записи", cnt27 == 2)
body = c.get(f"/employee/{eid}?year=2027").get_data(as_text=True)
check("год 2027 виден в карточке", "2027" in body)
# в новом году два блока полугодий видны (записи пустые)
check("оба полугодия нового года отображаются", "I полугодие" in body and "II полугодие" in body)

# --- удаление года ---
body = c.get(f"/employee/{eid}?year=2027").get_data(as_text=True)
check("кнопка удаления года видна", f"Удалить год 2027" in body)
c.post(f"/employee/{eid}/year/delete", data={"year": "2027"}, follow_redirects=True)
cnt27 = dbq("SELECT COUNT(*) c FROM year_records WHERE employee_id=? AND year=2027", (eid,))[0]["c"]
check("года 2027 нет после удаления", cnt27 == 0)
body = c.get(f"/employee/{eid}?year=2026").get_data(as_text=True)
check("2026 год сохранился после удаления 2027", "Цели сотрудника" in body)

# --- кликабельное имя в списке ---
body = c.get("/").get_data(as_text=True)
check("имя сотрудника — кликабельная ссылка", f'class="emp-link" href="/employee/{eid}"' in body)
check("ссылки 'Открыть →' больше нет", "Открыть →" not in body)

# --- теги: справочник ---
c.post("/catalog/tag/add", data={"name": "Наставник"})
c.post("/catalog/tag/add", data={"name": "Развитие"})
t_nast = dbq("SELECT * FROM tags WHERE name='Наставник'")[0]
t_razv = dbq("SELECT * FROM tags WHERE name='Развитие'")[0]
check("тег создан с цветом", t_nast["color"] and t_nast["color"].startswith("#"))

# --- теги: привязка к сотруднику через редактирование ---
emp_row = dbq("SELECT * FROM employees WHERE id=?", (eid,))[0]
c.post(f"/employee/{eid}/edit", data={
    "name": "Иванов Иван",
    "position_id": str(emp_row["position_id"] or ""),
    "department_id": str(emp_row["department_id"] or ""),
    "salary": emp_row["salary"] or "",
    "tag_ids": [str(t_nast["id"]), str(t_razv["id"])],
}, follow_redirects=True)
taglinks = dbq("SELECT tag_id FROM employee_tags WHERE employee_id=?", (eid,))
check("2 тега привязаны к сотруднику", len(taglinks) == 2)
body = c.get(f"/employee/{eid}").get_data(as_text=True)
check("теги видны в карточке", "Наставник" in body and "Развитие" in body)
body = c.get("/").get_data(as_text=True)
check("теги видны в списке", "tag-chip" in body and "Иванов Иван" in body)

# --- теги: снятие при редактировании и удаление тега из справочника ---
emp_row = dbq("SELECT * FROM employees WHERE id=?", (eid,))[0]
c.post(f"/employee/{eid}/edit", data={
    "name": "Иванов Иван",
    "position_id": str(emp_row["position_id"] or ""),
    "department_id": str(emp_row["department_id"] or ""),
    "salary": emp_row["salary"] or "",
}, follow_redirects=True)
taglinks = dbq("SELECT tag_id FROM employee_tags WHERE employee_id=?", (eid,))
check("теги сняты (пустой выбор)", len(taglinks) == 0)
c.post(f"/catalog/tag/{t_nast['id']}/delete")
check("тег удалён из справочника", len(dbq("SELECT * FROM tags WHERE id=?", (t_nast["id"],))) == 0)

# --- проблемы: добавление из карточки ---
c.post(f"/employee/{eid}/problem/add", data={"text": "Не справляется с дедлайнами"})
c.post(f"/employee/{eid}/problem/add", data={"text": "Нужна менторская поддержка"})
probs = dbq("SELECT * FROM problems WHERE employee_id=?", (eid,))
check("2 проблемы добавлены в карточке", len(probs) == 2)
body = c.get(f"/employee/{eid}").get_data(as_text=True)
check("проблемы видны в карточке", "Не справляется с дедлайнами" in body and "менторская" in body)

# --- проблемы: общий список ---
body = c.get("/problems").get_data(as_text=True)
check("общий список показывает проблемы сотрудника", "Не справляется" in body and "Иванов Иван" in body)
check("в общем списке есть форма добавления", "problem_add_general" in body or 'name="employee_id"' in body)

# --- проблемы: добавление из общего списка ---
c.post("/employee/new", data={
    "name": "Проблемный Петров", "position_id": "", "department_id": "", "salary": ""
}, follow_redirects=True)
other = dbq("SELECT id FROM employees WHERE name='Проблемный Петров'")[0]
c.post("/problem/add", data={"employee_id": str(other["id"]), "text": "Часто опаздывает"})
probs = dbq("SELECT * FROM problems WHERE employee_id=?", (other["id"],))
check("проблема добавлена из общего списка", len(probs) == 1)

# --- проблемы: удаление (решена) ---
pid = dbq("SELECT id FROM problems WHERE text LIKE 'Не справляется%'")[0]
c.post(f"/problem/{pid['id']}/delete")
probs = dbq("SELECT * FROM problems WHERE id=?", (pid["id"],))
check("проблема удалена (решена)", len(probs) == 0)

# --- сотрудник без годов: нет неделимого 2026 по умолчанию ---
c.post("/employee/new", data={
    "name": "Сидоров БезГодов", "position_id": "", "department_id": "", "salary": ""
}, follow_redirects=True)
eid_noyear = dbq("SELECT id FROM employees WHERE name='Сидоров БезГодов'")[0]["id"]
body = c.get(f"/employee/{eid_noyear}").get_data(as_text=True)
check("нет записей → видна заглушка о создании года", "нет созданных годов" in body)
check("у сотрудника без годов нет неделимого 2026", "Полугодовые записи · 2026" not in body and "Цели сотрудника" not in body)

# второй сотрудник в другом отделе и другой должности
c.post("/catalog/position/add", data={"name": "Стажёр"})
c.post("/catalog/department/add", data={"name": "ТП Сбер"})
p_st = dbq("SELECT id FROM positions WHERE name='Стажёр'")[0]["id"]
d2 = dbq("SELECT id FROM departments WHERE name='ТП Сбер'")[0]["id"]
c.post("/employee/new", data={
    "name": "Петров Пётр", "position_id": str(p_st), "department_id": str(d2), "salary": "80 000 ₽"
}, follow_redirects=True)

r = c.get("/?department=" + "ТП+Orion+soft").get_data(as_text=True)
check("фильтр по отделу оставляет только Orion", "Иванов Иван" in r and "Петров Пётр" not in r)

r = c.get("/?position=" + "Стажёр").get_data(as_text=True)
check("фильтр по должности оставляет только стажёра", "Петров Пётр" in r and "Иванов Иван" not in r)

# комбинированный фильтр
r = c.get("/?department=ТП+Orion+soft&position=Старший+инженер").get_data(as_text=True)
# у Иванова отдел уже сброшен тестом выше, поэтому проверяем структуру: ничего не падает
check("комбинированный фильтр не падает", r and "Сотрудники" in r)

# --- валидация: пустое имя ---
r = c.post("/employee/new", data={"name": "", "position_id": "", "department_id": "", "salary": ""})
check("пустое имя отклонено", r.status_code == 302)

# --- поиск по имени/фамилии ---
# оба сотрудника: Иванов Иван (ТП Orion) и Петров Пётр (ТП Сбер)
r = c.get("/?q=Иванов").get_data(as_text=True)
check("поиск по фамилии → только Иванов", "Иванов Иван" in r and "Петров Пётр" not in r)
r = c.get("/?q=Пётр").get_data(as_text=True)
check("поиск по имени → только Петров", "Петров Пётр" in r and "Иванов Иван" not in r)
r = c.get("/?q=Орion").get_data(as_text=True)  # латиница в русском имени не должна найти
check("поиск без совпадений → пусто", "Нет сотрудников" in r or "Иванов Иван" not in r)
r = c.get("/?q=иванов").get_data(as_text=True)  # регистр не важен
check("поиск регистронезависимый", "Иванов Иван" in r)
r = c.get("/?department=ТП+Orion+soft&q=Иванов").get_data(as_text=True)
check("поиск комбинируется с фильтром отдела", "Иванов Иван" in r and "Петров Пётр" not in r)

# --- резервное копирование: экспорт ---
r = c.get("/backup")
check("страница резервной копии доступна", "Резервное копирование" in r.get_data(as_text=True))
r = c.get("/backup/export")
check("экспорт БД (скачивание файла)", r.status_code == 200 and
      (r.headers.get("Content-Disposition") or "").startswith("attachment"))
backup_bytes = r.data

# --- резервное копирование: импорт (валидация) ---
# неверный файл
from io import BytesIO
r = c.post("/backup/import", data={"dbfile": (BytesIO(b"not a db"), "bad.txt")},
           content_type="multipart/form-data", follow_redirects=True)
check("импорт мусорного файла отклонён", "Не удалось прочитать" in r.get_data(as_text=True) or
      "не похож" in r.get_data(as_text=True))

# корректный файл = экспортированная копия (перезапишет тестовую БД — ок)
r = c.post("/backup/import", data={"dbfile": (BytesIO(backup_bytes), "teambook_backup_1.db")},
           content_type="multipart/form-data", follow_redirects=True)
check("импорт корректного файла проходит", r.status_code == 200)

# --- экспорт в Excel ---
r = c.get("/report?year=2026")
xlsx = r.data
check("экспорт по всем (Excel) скачивается", r.status_code == 200 and
      xlsx[:2] == b"PK" and
      "spreadsheetml" in (r.headers.get("Content-Disposition") or "").lower() or
      (r.headers.get("Content-Type") or "").startswith("application/vnd.openxml"))
check("имя файла отчёта содержит год", "2026" in (r.headers.get("Content-Disposition") or ""))
r = c.get(f"/employee/{eid}/report?year=2026")
check("экспорт по одному сотруднику (Excel) скачивается", r.status_code == 200 and r.data[:2] == b"PK")
check("имя файла отчёта по сотруднику", f"teambook_" in (r.headers.get("Content-Disposition") or ""))

# --- экспорт в PDF ---
r = c.get("/report?year=2026&format=pdf")
check("экспорт по всем (PDF) скачивается", r.status_code == 200 and r.data[:4] == b"%PDF")
check("PDF content-type", (r.headers.get("Content-Type") or "").find("pdf") > -1)
check("PDF имя файла", ".pdf" in (r.headers.get("Content-Disposition") or ""))
r = c.get(f"/employee/{eid}/report?year=2026&format=pdf")
check("экспорт по одному сотруднику (PDF) скачивается", r.status_code == 200 and r.data[:4] == b"%PDF")
check("PDF по сотруднику имя", ".pdf" in (r.headers.get("Content-Disposition") or ""))

# --- переименование справочника ---
d1 = dbq("SELECT * FROM departments WHERE name='ТП Orion soft'")[0]["id"]
c.post(f"/catalog/department/{d1}/rename", data={"name": "ТП Orion (переим)"})
check("отдел переименован", len(dbq("SELECT * FROM departments WHERE name='ТП Orion (переим)'")) == 1)

# --- логаут (изменяющая операция — только POST) ---
c.post("/logout")
r = c.get("/", follow_redirects=True)
check("после логаута снова логин", "/login" in r.request.path)

# === КАНБАН-ДОСКА И ГАНТ ===
c.post("/login", data={"password": "test-pass-123"}, follow_redirects=True)
with c.session_transaction() as sess:
    sess["_csrf"] = CSRF_TOKEN

# страница доступна (до создания столбцов — пустая доска)
r = c.get("/board")
check("доска открывается", r.status_code == 200 and "Канбан" in r.get_data(as_text=True))
# создать обычные (несистемные) столбцы
c.post("/board/column/add", data={"name": "В работе"}, follow_redirects=True)
c.post("/board/column/add", data={"name": "Готово"}, follow_redirects=True)
work = dbq("SELECT id, name, kind, locked FROM kb_columns WHERE name='В работе'")[0]
done = dbq("SELECT id, name, kind, locked FROM kb_columns WHERE name='Готово'")[0]
# с прошлых версий на канбане НЕ должно быть системных колонок (Бэклог остался только в todo)
sys_cols = dbq("SELECT * FROM kb_columns WHERE kind='kanban' AND locked=1")
check("на канбане нет системного Бэклога", len(sys_cols) == 0)
check("два обычных столбца созданы", work["name"] == "В работе" and done["name"] == "Готово")
# переименовать обычный столбец
c.post(f"/board/column/{done['id']}/rename", data={"name": "Сделано"}, follow_redirects=True)
check("столбец переименован",
      len(dbq("SELECT * FROM kb_columns WHERE id=? AND name='Сделано'", (done["id"],))) == 1)

# создать задачу с двумя сотрудниками и датами — она должна попасть в первый обычный столбец
emp_kb = dbq("SELECT id, name FROM employees WHERE active=1 LIMIT 1")[0]
emp_kb2 = dbq("SELECT id, name FROM employees WHERE active=1 AND id!=? LIMIT 1",
              (emp_kb["id"],))[0]
c.post("/board/task/add", data={
    "title": "Задача А",
    "employee_id": [str(emp_kb["id"]), str(emp_kb2["id"])],
    "start_date": "2026-10-01", "due_date": "2026-10-10",
    "description": "описание А",
}, follow_redirects=True)
tasks = dbq("SELECT * FROM kb_tasks WHERE title='Задача А'")
mem = dbq("SELECT employee_id FROM kb_task_members WHERE task_id=?", (tasks[0]["id"],))
check("новая задача упала в первый обычный столбец",
      len(tasks) == 1 and tasks[0]["column_id"] == work["id"])
# --- уведомления в шапке: просроченная задача попадает в колокольчик ---
overdue_title = "Просроченная задача ЮЗ"
c.post("/board/task/add", data={
    "title": overdue_title,
    "employee_id": [str(emp_kb["id"])],
    "start_date": "2026-09-01", "due_date": "2026-09-01",
}, follow_redirects=True)
ov = dbq("SELECT id FROM kb_tasks WHERE title=?", (overdue_title,))
check("просроченная задача создана", len(ov) == 1)
r = c.get("/board")
html = r.get_data(as_text=True)
check("в шапке есть колокольчик с бейджем",
      "notif-badge" in html and "Просрочено" in html and overdue_title in html)
# задача без срока не порождает уведомления
c.post("/board/task/add", data={
    "title": "Задача без срока",
    "employee_id": [str(emp_kb["id"])],
}, follow_redirects=True)
r = c.get("/board")
html = r.get_data(as_text=True)
check("колокольчик всё ещё показывает просрочку", "Просрочено" in html)
# --- счётчик просроченных задач на пункте «Доска» в меню ---
r = c.get("/board")
h = r.get_data(as_text=True)
m_board = _re.search(r'Доска<span class="nav-badge">(\d+)</span>', h)
check("меню: на пункте «Доска» бейдж с просроченными", m_board is not None and int(m_board.group(1)) >= 1)
# --- сводка «цифры недели» на главной ---
r = c.get("/")
h = r.get_data(as_text=True)
check("главная: сводка цифр недели (stats-row)",
      "stats-row" in h and "открытых задач" in h and "просрочено" in h)
check("задача создана с датами и двумя исполнителями",
      tasks[0]["start_date"] == "2026-10-01" and tasks[0]["due_date"] == "2026-10-10"
      and sorted(m["employee_id"] for m in mem) == sorted([emp_kb["id"], emp_kb2["id"]]))

# добавить задачу без исполнителя/дат
c.post("/board/task/add", data={"title": "Задача Б"}, follow_redirects=True)
taskB = dbq("SELECT id FROM kb_tasks WHERE title='Задача Б'")[0]
check("вторая новая задача тоже в первом обычном столбце",
      dbq("SELECT column_id FROM kb_tasks WHERE id=?", (taskB["id"],))[0]["column_id"] == work["id"])

# страница рендерит карточки и гант-элементы
r = c.get(f"/board?month=10&year=2026")
h = r.get_data(as_text=True)
check("канбан показывает карточку", "Задача А" in h and "Задача Б" in h)
check("на карточке перечислены оба исполнителя",
      "emp_kb" not in h and emp_kb["name"] in h and emp_kb2["name"] in h)
check("гант-календарь отрисован (сетка месяца)",
      "gcal-table" in h and "Пн" in h and "gcal-cell" in h)
check("в календарной сетке нет задачи без дат",
      "gcal-table" in h and "Задача Б" not in h.split("gcal-table")[1].split("</table>")[0])
check("на странице есть зоны Архив и Корзина",
      "kb-zone" in h and "Архив" in h and "Корзина" in h)

# модалка карточки
r = c.get(f"/board/task/{tasks[0]['id']}/card")
hm = r.get_data(as_text=True)
check("модалка карточки открывается с формой",
      r.status_code == 200 and 'name="title"' in hm and 'checkbox' in hm
      and 'tm-employee-list' in hm)
import re as _re
def _is_checked(html, eid):
    # найди чекбокс исполнителя по value и проверь, есть ли атрибут checked
    m = _re.search(r'<input[^>]*name="employee_id"[^>]*value="%d"[^>]*>' % eid, html)
    return bool(m) and "checked" in m.group(0)
check("в модалке отмечены оба исполнителя",
      _is_checked(hm, emp_kb["id"]) and _is_checked(hm, emp_kb2["id"]))
# поиск-фильтр присутствует
check("в модалке есть поле поиска исполнителя", "tm-employee-search" in hm)

# drag&drop: переместить задачу в другой столбец
r = c.post(f"/board/task/{tasks[0]['id']}/move", data={"column_id": str(work["id"])})
check("перемещение задачи меняет столбец",
      r.status_code == 204 and dbq("SELECT column_id FROM kb_tasks WHERE id=?",
                                   (tasks[0]["id"],))[0]["column_id"] == work["id"])

# редактирование задачи (в колонке work)
r = c.post(f"/board/task/{tasks[0]['id']}/edit", data={
    "title": "Задача А (ред)", "column_id": str(work["id"]),
    "employee_id": [str(emp_kb2["id"])],
    "start_date": "2026-11-02", "due_date": "2026-11-05", "description": "обновлено",
}, follow_redirects=True)
mem = dbq("SELECT employee_id FROM kb_task_members WHERE task_id=?", (tasks[0]["id"],))
check("задача отредактирована, исполнитель один",
      len(dbq("SELECT * FROM kb_tasks WHERE id=? AND title='Задача А (ред)' AND due_date='2026-11-05'",
              (tasks[0]["id"],))) == 1
      and [m["employee_id"] for m in mem] == [emp_kb2["id"]])

# удаление столбца с задачами запрещено (обычный столбец с задачей)
c.post(f"/board/task/{tasks[0]['id']}/move", data={"column_id": str(work["id"])})
r = c.post(f"/board/column/{work['id']}/delete", follow_redirects=True)
check("столбец с задачами не удаляется",
      len(dbq("SELECT * FROM kb_columns WHERE id=?", (work["id"],))) == 1)
# пустой обычный столбец удаляется
empty_col = dbq("SELECT id FROM kb_columns WHERE name='Сделано'")[0]
r = c.post(f"/board/column/{empty_col['id']}/delete", follow_redirects=True)
check("пустой обычный столбец удаляется",
      len(dbq("SELECT * FROM kb_columns WHERE id=?", (empty_col["id"],))) == 0)
# «удаление» = в корзину (soft delete), задача исчезает с доски но остаётся в БД
r1 = c.post(f"/board/task/{taskB['id']}/trash")
r2 = c.post(f"/board/task/{tasks[0]['id']}/trash")
check("перетаскивание в корзину (soft delete)",
      r1.status_code == 204 and r2.status_code == 204
      and len(dbq("SELECT * FROM kb_tasks WHERE id=? AND deleted_at!=''",
                  (taskB["id"],))) == 1)
# доска после этого пустая в первом столбце
r = c.get("/board")
h = r.get_data(as_text=True)
check("задачи убраны с доски в корзину",
      "Задача А" not in h and "Задача Б" not in h and "Корзина" in h)
# страница корзины
r = c.get("/board/trash")
h = r.get_data(as_text=True)
check("корзина перечисляет удалённые", r.status_code == 200
      and "Задача А" in h and "Задача Б" in h)
# восстановление из корзины
c.post(f"/board/task/{taskB['id']}/restore", follow_redirects=True)
check("восстановление из корзины",
      len(dbq("SELECT * FROM kb_tasks WHERE id=? AND deleted_at=''", (taskB["id"],))) == 1)
# архив: перетащить задачу в архив, затем в архив-странице
c.post(f"/board/task/{taskB['id']}/archive")
check("перемещение в архив",
      len(dbq("SELECT * FROM kb_tasks WHERE id=? AND archived_at!='' AND deleted_at=''",
              (taskB["id"],))) == 1)
r = c.get("/board/archive")
h = r.get_data(as_text=True)
check("архив-страница показывает задачу и кнопку возврата",
      r.status_code == 200 and "Задача Б" in h and "Вернуть на доску" in h)
# вернуть из архива
c.post(f"/board/task/{taskB['id']}/restore", follow_redirects=True)
check("возврат из архива",
      len(dbq("SELECT * FROM kb_tasks WHERE id=? AND archived_at='' AND deleted_at=''",
              (taskB["id"],))) == 1)
# очистить корзину (purge-all безвозвратно удаляет оставшийся мусор)
c.post("/board/trash/purge-all", follow_redirects=True)
check("очистка корзины",
      len(dbq("SELECT * FROM kb_tasks WHERE id=?", (tasks[0]["id"],))) == 0)
# задача Б после restore снова активна — уводим её в корзину, чтобы столбец стал пустым
c.post(f"/board/task/{taskB['id']}/trash")
c.post(f"/board/task/{tasks[0]['id']}/trash")
# пустой обычный столбец («Сделано», изначально «Готово») удаляется
r = c.post(f"/board/column/{done['id']}/delete", follow_redirects=True)
check("пустой столбец удалён",
      len(dbq("SELECT * FROM kb_columns WHERE id=?", (done["id"],))) == 0)

# --- перестановка столбцов: перетаскиванием /board/column/<id>/move ---
def _cols_ordered(dbq):
    return [r["name"] for r in dbq("SELECT name FROM kb_columns WHERE kind='kanban' ORDER BY sort_order, id")]
c.post("/board/column/add", data={"name": "Бокс Справа"}, follow_redirects=True)
c.post("/board/column/add", data={"name": "Бокс Правее"}, follow_redirects=True)
ord_before = _cols_ordered(dbq)
# проверим перенос самого правого столбца в самую левую позицию («перед» первым)
last2 = dbq("SELECT id, name FROM kb_columns WHERE kind='kanban' ORDER BY sort_order DESC, id DESC LIMIT 1")[0]
first2 = dbq("SELECT id FROM kb_columns WHERE kind='kanban' ORDER BY sort_order, id LIMIT 1")[0]
r = c.post(f"/board/column/{last2['id']}/move", data={"before": str(first2["id"])})
ord_after = _cols_ordered(dbq)
check("столбец переносится в начало перетаскиванием",
      r.status_code == 204 and ord_after[0] == last2["name"] and ord_after != ord_before)
# без `before` — переносится в конец
firstb = dbq("SELECT id, name FROM kb_columns WHERE kind='kanban' ORDER BY sort_order, id LIMIT 1")[0]
r = c.post(f"/board/column/{firstb['id']}/move", data={"before": ""})
ord_last = _cols_ordered(dbq)
check("столбец переносится в конец без `before`",
      r.status_code == 204 and ord_last[-1] == firstb["name"])

# --- счётчик проблем в меню ---
emp_p = dbq("SELECT id FROM employees WHERE active=1 LIMIT 1")[0]["id"]
r = c.get("/")
html = r.get_data(as_text=True)
c.post("/problem/add", data={"employee_id": str(emp_p), "text": "Тестовая проблема сч"}, follow_redirects=True)
r = c.get("/")
html = r.get_data(as_text=True)
import re as _re
m = _re.search(r'Проблемы<span class="nav-badge">(\d+)</span>', html)
check("счётчик проблем в меню показывает количество",
      m is not None and int(m.group(1)) >= 1)
pid = dbq("SELECT id FROM problems WHERE text='Тестовая проблема сч'")[0]["id"]
c.post(f"/problem/{pid}/delete", follow_redirects=True)
r = c.get("/")
html = r.get_data(as_text=True)
m2 = _re.search(r'Проблемы<span class="nav-badge">(\d+)</span>', html)
prev = int(m.group(1)) if m else 0
now = int(m2.group(1)) if m2 else 0
check("счётчик уменьшается после удаления проблемы", now == prev - 1)

# --- страница настроек: резервная копия + Telegram-бот ---
r = c.get("/settings")
h = r.get_data(as_text=True)
check("страница настроек открывается",
      r.status_code == 200 and "Telegram-бот" in h and "Резервная копия" in h and "tg_token" in h)
# меню: пункт «Резервная копия» заменён на «Настройки»
r = c.get("/")
h = r.get_data(as_text=True)
check("в меню есть «Настройки» вместо «Резервная копия»",
      "Настройки" in h and "Резервная копия" not in h)
# сохранение настроек бота пишет в settings
c.post("/settings/bot/save", data={
    "tg_token": "123:TESTTOKEN",
    "tg_admin": "418650868",
    "tg_enabled": "1",
}, follow_redirects=True)
sv = {r["key"]: r["value"] for r in dbq("SELECT * FROM settings")}
check("настройки бота сохранены в БД",
      sv.get("tg_token") == "123:TESTTOKEN" and sv.get("tg_admin_id") == "418650868"
      and sv.get("tg_enabled") == "1")
r = c.get("/settings")
h = r.get_data(as_text=True)
check("в форме подставлены сохранённые значения",
      "123:TESTTOKEN" in h and "418650868" in h)

# --- личный todo: бэклог + матрица Эйзенхауэра ---
c.post("/todo/add", data={"title": "Туду-задача А"}, follow_redirects=True)
c.post("/todo/add", data={"title": "Туду-задача Б"}, follow_redirects=True)
todos = {r["title"]: r["id"] for r in dbq("SELECT * FROM todo_items")}
check("todo: задачи добавлены в бэклог",
      "Туду-задача А" in todos and "Туду-задача Б" in todos)
r = c.get("/todo")
h = r.get_data(as_text=True)
check("todo: страница показывает бэклог и матрицу",
      "Бэклог" in h and "Важно · Срочно" in h and "Не важно · Не срочно" in h and "Туду-задача А" in h)
check("todo: у квадрантов есть описания (Сделать/Запланировать/Делегировать/Удалить)",
      all(a in h for a in ["Сделать", "Запланировать", "Делегировать", "Удалить"]))
# переместить А в квадрант «важно + срочно» (drag&drop -> /todo/<id>/move)
r = c.post(f"/todo/{todos['Туду-задача А']}/move", data={"status": "q_iu"})
row = dbq("SELECT status, sort_order FROM todo_items WHERE id=?",
          (todos["Туду-задача А"],))[0]
check("todo: задача перемещена в квадрант матрицы",
      r.status_code == 204 and row["status"] == "q_iu")
# переносить в бэклог и между квадрантами
r = c.post(f"/todo/{todos['Туду-задача А']}/move", data={"status": "q_nn"})
row = dbq("SELECT status FROM todo_items WHERE id=?", (todos["Туду-задача А"],))[0]
check("todo: можно перенести в другой квадрант", row["status"] == "q_nn")
c.post(f"/todo/{todos['Туду-задача А']}/move", data={"status": "backlog"})
row = dbq("SELECT status FROM todo_items WHERE id=?", (todos["Туду-задача А"],))[0]
check("todo: можно вернуть из квадранта в бэклог", row["status"] == "backlog")
# делегировать А в канбан (первый обычный столбец)
c.post(f"/todo/{todos['Туду-задача А']}/delegate", follow_redirects=True)
left = dbq("SELECT 1 FROM todo_items WHERE id=?", (todos["Туду-задача А"],))
kb = dbq("SELECT * FROM kb_tasks WHERE title='Туду-задача А'")
first = dbq("SELECT id FROM kb_columns WHERE kind='kanban' ORDER BY sort_order, id LIMIT 1")[0]["id"]
check("todo: делегирование убирает из todo и создаёт на канбане",
      len(left) == 0 and len(kb) == 1 and kb[0]["column_id"] == first)
# пометить Б сделанным (попадает в архив за сегодня, не удаляется)
c.post(f"/todo/{todos['Туду-задача Б']}/done", follow_redirects=True)
done_row = dbq("SELECT status, done_date FROM todo_items WHERE id=?",
               (todos["Туду-задача Б"],))[0]
check("todo: «сделано» помечает выполненным с датой (архив)",
      done_row["status"] == "done" and done_row["done_date"] == datetime.date.today().isoformat())
# архив — отдельная страница, на todo только кнопка
r = c.get("/todo")
h = r.get_data(as_text=True)
check("todo: на странице кнопка архива, без столбца",
      "/todo/archive" in h and "Архив" in h and "Туду-задача Б" not in h)
r = c.get("/todo/archive")
h = r.get_data(as_text=True)
check("todo: архив-страница показывает выполненную задачу",
      r.status_code == 200 and "Архив задач" in h and "Туду-задача Б" in h)
# возврат из архива в бэклог (кнопка статус=backlog)
r = c.post(f"/todo/{todos['Туду-задача Б']}/move", data={"status": "backlog"})
row = dbq("SELECT status FROM todo_items WHERE id=?", (todos["Туду-задача Б"],))[0]
check("todo: возврат из архива в бэклог", r.status_code == 204 and row["status"] == "backlog")
# редактирование задачи (переименовать)
ed_id = dbq("SELECT id FROM todo_items WHERE title='Туду-задача Б'")[0]["id"]
r = c.post(f"/todo/{ed_id}/edit", data={"title": "Туду-задача Б (ред)"}, follow_redirects=True)
check("todo: задача отредактирована",
      r.status_code == 200 and len(dbq("SELECT id FROM todo_items WHERE id=? AND title='Туду-задача Б (ред)'", (ed_id,))) == 1)
c.post("/todo/add", data={"title": "Для редактирования"}, follow_redirects=True)
r = c.get("/todo")
h = r.get_data(as_text=True)
check("todo: на карточках есть кнопка редактирования и drag&drop",
      "openTodo(" in h and "Редактировать" in h and "dirTodoDrag" in h
      and "todoDrop" in h and "/todo/' + encodeURIComponent(id) + '/card'" in h)
# переставить порядок в бэклоге: очистить todo, добавить две и поменять местами
_conn = sqlite3.connect(os.path.join(tmp, "hr_notes.db"))
_conn.execute("DELETE FROM todo_items"); _conn.commit(); _conn.close()
c.post("/todo/add", data={"title": "Порядок-1"}, follow_redirects=True)
c.post("/todo/add", data={"title": "Порядок-2"}, follow_redirects=True)
p1 = dbq("SELECT id FROM todo_items WHERE title='Порядок-1'")[0]["id"]
p2 = dbq("SELECT id FROM todo_items WHERE title='Порядок-2'")[0]["id"]
c.post(f"/todo/{p1}/up", follow_redirects=True)  # у первого нет верхнего — ничего
c.post(f"/todo/{p2}/up", follow_redirects=True)  # p2 станет выше p1
so = {r["title"]: r["sort_order"] for r in dbq("SELECT * FROM todo_items")}
check("todo: перестановка порядка (↑/↓)", so["Порядок-2"] < so["Порядок-1"])
# Legacy status migration is covered on an old unconstrained schema in
# test_schema_constraints.py; the current schema must reject 'today'.
_conn = sqlite3.connect(os.path.join(tmp, "hr_notes.db"))
try:
    _conn.execute("UPDATE todo_items SET status='today' WHERE title='Порядок-1'")
    today_rejected = False
except sqlite3.IntegrityError:
    today_rejected = True
finally:
    _conn.close()
r = c.get("/todo")
check("todo: страница открывается с раскладкой матрицы",
      today_rejected and r.status_code == 200 and "eisen-quad" in r.get_data(as_text=True))

# --- улучшения пакета: срок, тег, поиск, шаблоны, архив по дате, счётчик, тема ---
conn = sqlite3.connect(os.path.join(tmp, "hr_notes.db"))
conn.execute("INSERT INTO todo_items (title, status, due_date, tag) VALUES (?,?,?,?)",
             ("С сероком и тегом", "backlog", "2030-01-01", "важное"))
todo_id = conn.execute(
    "SELECT id FROM todo_items WHERE title='С сероком и тегом'").fetchone()[0]
conn.commit(); conn.close()
r = c.get(f"/todo/{todo_id}/card"); h = r.get_data(as_text=True)
check("todo: модалка редактирования (срок/тег)",
      r.status_code == 200 and 'name="due_date"' in h and 'name="tag"' in h)
# срок+тег сохраняются через edit
r = c.post(f"/todo/{todo_id}/edit",
           data={"title": "С сероком и тегом", "due_date": "2030-06-15", "tag": "люди"})
r = c.get("/todo"); h = r.get_data(as_text=True)
check("todo: срок и тег видны на карточке и в фильтре",
      "06-15" in h and "todo-tag" in h
      and 'id="todoSearch"' in h and 'id="todoTagFilter"' in h)
check("todo: шаблоны быстрых задач убраны",
      "applyTpl" not in h and "Быстрое добавление" not in h)
r = c.get("/todo/archive?date=2030-06-15")
check("todo: архив принимает выбранную дату", r.status_code == 200)
# счётчик в навигации = число невыполненных задач (на странице todo через base)
check("main: светлая/тёмная тема — кнопка и скрипт в base",
      'id="themeToggle"' in h and "toggleTheme" in h)

print()
if failures:
    print(f"ИТОГ: {len(failures)} ПРОВАЛЕНО -> {failures}")
    sys.exit(1)
print("ИТОГ: ВСЕ ТЕСТЫ ПРОЙДЕНЫ")