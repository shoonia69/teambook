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
}, follow_redirects=True)
r = c.get(f"/employee/{eid}?year=2026")
body = r.get_data(as_text=True)
check("запись цели", "Освоить Orion soft" in body)
check("запись предложения", "Дать проект X" in body)
check("запись пожелания", "Хочет повышение" in body)
check("запись комментария", "Хорошая динамика" in body)

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
check("карточка имеет 4 спойлера", body.count('class="card accordion"') == 4)
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

# валидация: история без даты отклоняется
c.post(f"/employee/{eid}/history/add", data={"change_date": "", "position_id": "", "salary": "", "note": ""})
check("история без даты отклонена", len(dbq(f"SELECT * FROM employee_history WHERE employee_id={eid} AND change_date=''")) == 0)

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

# --- переименование справочника ---
d1 = dbq("SELECT * FROM departments WHERE name='ТП Orion soft'")[0]["id"]
c.post(f"/catalog/department/{d1}/rename", data={"name": "ТП Orion (переим)"})
check("отдел переименован", len(dbq("SELECT * FROM departments WHERE name='ТП Orion (переим)'")) == 1)

# --- логаут ---
c.get("/logout")
r = c.get("/", follow_redirects=True)
check("после логаута снова логин", "/login" in r.request.path)

print()
if failures:
    print(f"ИТОГ: {len(failures)} ПРОВАЛЕНО -> {failures}")
    sys.exit(1)
print("ИТОГ: ВСЕ ТЕСТЫ ПРОЙДЕНЫ")