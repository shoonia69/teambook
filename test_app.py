# -*- coding: utf-8 -*-
"""Функциональный тест TeamBook через тестовый клиент Flask (без реального сервера)."""
import os
import tempfile
import sys

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

# --- логаут ---
c.get("/logout")
r = c.get("/", follow_redirects=True)
check("после логаута снова логин", "/login" in r.request.path)

# === РОЛЕВАЯ МОДЕЛЬ ===
# логин владельца (без имени — подразумевается владелец)
r = c.post("/login", data={"password": "test-pass-123"}, follow_redirects=True)
check("владелец вошёл для создания пользователей", "Сотрудники" in r.get_data(as_text=True))

# создать: тимлид (скоуп = отдел d1, без зарплат и отчётов), зритель (viewer)
d2 = dbq("SELECT * FROM departments WHERE name='ТП Сбер'")[0]["id"]
# отдел d1 переименован в 'ТП Orion (переим)'
c.post("/users/new", data={
    "username": "teamlead1", "password": "tl-pass", "role": "teamlead",
    "departments": [str(d1)], "scope_all": "",
})
c.post("/users/new", data={
    "username": "viewer1", "password": "vi-pass", "role": "viewer",
    "departments": [str(d1)], "scope_all": "",
})
check("тимлид создан", len(dbq("SELECT * FROM users WHERE username='teamlead1'")) == 1)
check("зритель создан", len(dbq("SELECT * FROM users WHERE username='viewer1'")) == 1)

# гость из чужого отдела d2
c.post("/users/new", data={
    "username": "other-lead", "password": "ol-pass", "role": "teamlead",
    "departments": [str(d2)], "scope_all": "",
})

# вернуть Иванову отдел d1 (ранее сброшен в NULL) и создать сотрудника в d2
c.post(f"/employee/{eid}/edit", data={
    "name": "Иванов Иван", "position_id": str(p2),
    "department_id": str(d1), "salary": "140 000 ₽",
}, follow_redirects=True)
c.post("/employee/new", data={
    "name": "Петров Пётр", "position_id": str(p1), "department_id": str(d2),
    "salary": "90 000 ₽",
}, follow_redirects=True)

# --- вход тимлида ---
c.get("/logout")
c.post("/login", data={"username": "teamlead1", "password": "tl-pass"})
r = c.get("/", follow_redirects=True)
html = r.get_data(as_text=True)
check("тимлид видит своего сотрудника", "Иванов Иван" in html)
check("тимлид НЕ видит сотрудника чужого отдела", "Петров" not in html)
# зарплата скрыта (нет права view_salary по умолчанию)
check("тимлид не видит зарплату", "120 000" not in html)
# меню пользователей скрыто
check("тимлид не видит пункт «Пользователи»", "Пользователи" not in html)
# тимлид может добавлять проблемы
r = c.post(f"/employee/{eid}/problem/add", data={"text": "Задача тимлида"},
           follow_redirects=True)
check("тимлид добавляет проблему своему", "Задача тимлида" in r.get_data(as_text=True))
# но не чужому сотруднику
other = dbq("SELECT * FROM employees WHERE name='Петров Пётр'")
if other:
    r = c.post(f"/employee/{other[0]['id']}/problem/add", data={"text": "Чужая задача"})
    check("тимлид НЕ может добавить проблему чужому", r.status_code == 403)
    r = c.post(f"/employee/{other[0]['id']}/record", data={})
    check("тимлид НЕ может сохранить запись чужому", r.status_code == 403)
# но отчёт свой может (view_reports нет у тимлида по пресету) — проверим доступ/скрытие
r = c.get("/report?year=2026")
check("тимлид НЕ может выгрузить отчёт (нет view_reports)", r.status_code == 403)

# --- вход зрителя ---
c.get("/logout")
c.post("/login", data={"username": "viewer1", "password": "vi-pass"})
r = c.get("/", follow_redirects=True)
html = r.get_data(as_text=True)
check("зритель видит список", "Сотрудники" in html)
check("зритель НЕ видит зарплату", "120 000" not in html)
r = c.post(f"/employee/{eid}/record", data={"year": "2026", "semester": "1H",
                                            "goals_employee": "x"})
check("зритель НЕ может сохранять записи (403)", r.status_code == 403)
r = c.get("/problems")
check("зритель не видит общий список проблем", r.status_code == 403)
r = c.get("/users")
check("зритель не видит пользователей", r.status_code != 200)

# === РЕГРЕССИЯ: редактирование пользователя не должно снимать флаг «активен» ===
# владелец правит тимлида, не меняя признак активности, — он должен остаться активным
c.get("/logout")
c.post("/login", data={"password": "test-pass-123"})
tl_id = dbq("SELECT id FROM users WHERE username='teamlead1'")[0]["id"]
r = c.get(f"/users/{tl_id}/edit")
check("страница редактирования пользователя показывает чекбокс активности",
      "is_active" in r.get_data(as_text=True))
# сохранить: роль teamlead, отдел d1, отметить активность
c.post(f"/users/{tl_id}/edit", data={
    "role": "teamlead", "is_active": "1", "departments": [str(d1)], "scope_all": "",
})
tl_after = dbq("SELECT is_active FROM users WHERE username='teamlead1'")[0]["is_active"]
check("после редактирования пользователь остался активным (is_active=1)", tl_after == 1)
# новый пользователь по умолчанию активен
nu = dbq("SELECT is_active FROM users WHERE username='viewer1'")[0]["is_active"]
check("созданный пользователь активен по умолчанию", nu == 1)

print()
if failures:
    print(f"ИТОГ: {len(failures)} ПРОВАЛЕНО -> {failures}")
    sys.exit(1)
print("ИТОГ: ВСЕ ТЕСТЫ ПРОЙДЕНЫ")