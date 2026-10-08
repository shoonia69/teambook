import datetime
import io
import re
import sqlite3

import pytest


def db_rows(app_env, sql, args=()):
    connection = sqlite3.connect(app_env.DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, args).fetchall()
    finally:
        connection.close()


def post(client, url, data=None, **kwargs):
    with client.session_transaction() as session:
        token = session.setdefault("_csrf", "pytest-csrf-token")
    if data is None:
        data = {}
    if isinstance(data, dict):
        data = dict(data)
        data.setdefault("_csrf", token)
    return client.post(url, data=data, **kwargs)


@pytest.fixture
def catalogs(app_env, client):
    for name in ("Инженер 1 категории", "Старший инженер"):
        post(client, "/catalog/position/add", {"name": name})
    for name in ("ТП Orion soft", "ТП Сбер"):
        post(client, "/catalog/department/add", {"name": name})
    return {
        "position": db_rows(app_env, "SELECT id FROM positions WHERE name=?", ("Инженер 1 категории",))[0][0],
        "senior": db_rows(app_env, "SELECT id FROM positions WHERE name=?", ("Старший инженер",))[0][0],
        "department": db_rows(app_env, "SELECT id FROM departments WHERE name=?", ("ТП Orion soft",))[0][0],
    }


@pytest.fixture
def employee(app_env, client, catalogs):
    response = post(
        client,
        "/employee/new",
        {
            "name": "Иванов Иван",
            "position_id": str(catalogs["position"]),
            "department_id": str(catalogs["department"]),
            "salary": "120 000 ₽",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    return db_rows(app_env, "SELECT id FROM employees WHERE name=?", ("Иванов Иван",))[0][0]


def test_authentication_redirect_login_and_logout(client):
    post(client, "/logout")
    assert client.get("/", follow_redirects=True).request.path == "/login"
    assert client.post("/login", data={"password": "wrong"}).status_code == 200
    response = client.post("/login", data={"password": "test-password"}, follow_redirects=True)
    assert response.status_code == 200
    assert "Сотрудники" in response.get_data(as_text=True)


def test_catalog_creation_duplicate_and_reference_delete(app_env, client, catalogs, employee):
    assert len(db_rows(app_env, "SELECT * FROM positions")) == 2
    assert len(db_rows(app_env, "SELECT * FROM departments")) == 2
    post(client, "/catalog/position/add", {"name": "Инженер 1 категории"})
    assert len(db_rows(app_env, "SELECT * FROM positions")) == 2
    post(client, f"/catalog/position/{catalogs['position']}/delete")
    row = db_rows(app_env, "SELECT position_id FROM employees WHERE id=?", (employee,))[0]
    assert row["position_id"] is None


def test_employee_create_edit_clear_department_and_render(app_env, client, catalogs, employee):
    row = db_rows(app_env, "SELECT * FROM employees WHERE id=?", (employee,))[0]
    assert row["position_id"] == catalogs["position"]
    assert row["department_id"] == catalogs["department"]
    post(client, f"/employee/{employee}/edit", {
        "name": "Иванов Иван", "position_id": str(catalogs["senior"]),
        "department_id": "", "salary": "140 000 ₽", "hire_date": "2023-04-10",
    })
    row = db_rows(app_env, "SELECT * FROM employees WHERE id=?", (employee,))[0]
    assert row["position_id"] == catalogs["senior"]
    assert row["department_id"] is None
    assert row["hire_date"] == "2023-04-10"
    body = client.get(f"/employee/{employee}").get_data(as_text=True)
    assert "Старший инженер" in body
    assert "принят с 2023-04-10" in body
    home = client.get("/").get_data(as_text=True)
    assert f'class="emp-link" href="/employee/{employee}"' in home
    assert "Открыть →" not in home


def test_year_record_upsert_and_year_lifecycle(app_env, client, employee):
    payload = {
        "year": "2026", "semester": "1H", "goals_employee": "Освоить Orion soft",
        "proposals_manager": "Дать проект X", "wishes_employee": "Хочет повышение",
        "comments": "Хорошая динамика", "colleagues_feedback": "Коллеги высоко ценят",
    }
    post(client, f"/employee/{employee}/record", payload)
    payload["goals_employee"] = "Освоить Orion soft (v2)"
    post(client, f"/employee/{employee}/record", payload)
    rows = db_rows(app_env, "SELECT * FROM year_records WHERE employee_id=? AND year=2026 AND semester='1H'", (employee,))
    assert len(rows) == 1
    body = client.get(f"/employee/{employee}?year=2026").get_data(as_text=True)
    assert "Освоить Orion soft (v2)" in body
    assert "Коллеги высоко ценят" in body

    post(client, f"/employee/{employee}/year/new", {"year": "2027"})
    assert db_rows(app_env, "SELECT COUNT(*) c FROM year_records WHERE employee_id=? AND year=2027", (employee,))[0]["c"] == 2
    body = client.get(f"/employee/{employee}?year=2027").get_data(as_text=True)
    assert "I полугодие" in body and "II полугодие" in body
    assert "Удалить год 2027" in body
    post(client, f"/employee/{employee}/year/delete", {"year": "2027"})
    assert db_rows(app_env, "SELECT COUNT(*) c FROM year_records WHERE employee_id=? AND year=2027", (employee,))[0]["c"] == 0


def test_meeting_notes_and_history_crud(app_env, client, catalogs, employee):
    post(client, f"/employee/{employee}/meeting/new", {
        "date": "2026-02-10", "summary": "Обсудили задачи на квартал, договорились о менторстве."
    })
    post(client, f"/employee/{employee}/notes", {"notes": "Любит работу с API"})
    post(client, f"/employee/{employee}/notes", {"notes": "Обновлённая заметка"})
    post(client, f"/employee/{employee}/history/add", {
        "change_date": "2024-06-01", "position_id": str(catalogs["position"]),
        "salary": "150 000 ₽", "note": "повышение",
    })
    post(client, f"/employee/{employee}/history/add", {
        "change_date": "2025-01-15", "position_id": str(catalogs["senior"]),
        "salary": "160 000 ₽", "note": "главный инженер",
    })
    body = client.get(f"/employee/{employee}").get_data(as_text=True)
    assert "Обсудили задачи на квартал" in body
    assert "Обновлённая заметка" in body and "Любит работу с API" not in body
    assert body.count('class="card accordion"') == 4
    assert 'data-acc="notes" open' in body
    assert 'data-acc="history"' in body and 'data-acc="history" open' not in body
    assert body.find("2025-01-15") < body.find("2024-06-01")
    history_id = db_rows(app_env, "SELECT id FROM employee_history WHERE note='повышение'")[0][0]
    post(client, f"/history/{history_id}/edit", {
        "change_date": "2024-07-01", "position_id": str(catalogs["position"]),
        "salary": "155 000 ₽", "note": "повышение (правка)",
    })
    assert "повышение (правка)" in client.get(f"/employee/{employee}").get_data(as_text=True)
    post(client, f"/history/{history_id}/delete")
    assert not db_rows(app_env, "SELECT * FROM employee_history WHERE id=?", (history_id,))
    before = len(db_rows(app_env, "SELECT * FROM employee_history WHERE employee_id=?", (employee,)))
    post(client, f"/employee/{employee}/history/add", {"change_date": "", "position_id": "", "salary": "", "note": ""})
    assert len(db_rows(app_env, "SELECT * FROM employee_history WHERE employee_id=?", (employee,))) == before


def test_tags_and_problems_crud(app_env, client, catalogs, employee):
    post(client, "/catalog/tag/add", {"name": "Наставник"})
    post(client, "/catalog/tag/add", {"name": "Развитие"})
    tags = db_rows(app_env, "SELECT * FROM tags ORDER BY id")
    assert tags[0]["color"].startswith("#")
    post(client, f"/employee/{employee}/edit", {
        "name": "Иванов Иван", "position_id": str(catalogs["position"]),
        "department_id": str(catalogs["department"]), "salary": "120 000 ₽",
        "tag_ids": [str(row["id"]) for row in tags],
    })
    assert len(db_rows(app_env, "SELECT * FROM employee_tags WHERE employee_id=?", (employee,))) == 2
    assert all(name in client.get(f"/employee/{employee}").get_data(as_text=True) for name in ("Наставник", "Развитие"))
    post(client, f"/employee/{employee}/problem/add", {"text": "Не справляется с дедлайнами"})
    post(client, "/problem/add", {"employee_id": str(employee), "text": "Нужна менторская поддержка"})
    problems = db_rows(app_env, "SELECT * FROM problems WHERE employee_id=?", (employee,))
    assert len(problems) == 2
    body = client.get("/problems").get_data(as_text=True)
    assert "Не справляется" in body and "Иванов Иван" in body
    post(client, f"/problem/{problems[0]['id']}/delete")
    assert not db_rows(app_env, "SELECT * FROM problems WHERE id=?", (problems[0]["id"],))


def test_employee_search_and_filters(app_env, client, catalogs, employee):
    post(client, "/catalog/position/add", {"name": "Стажёр"})
    intern = db_rows(app_env, "SELECT id FROM positions WHERE name='Стажёр'")[0][0]
    second_department = db_rows(app_env, "SELECT id FROM departments WHERE name='ТП Сбер'")[0][0]
    post(client, "/employee/new", {
        "name": "Петров Пётр", "position_id": str(intern),
        "department_id": str(second_department), "salary": "80 000 ₽",
    })
    cases = [
        ("/?department=ТП+Orion+soft", "Иванов Иван", "Петров Пётр"),
        ("/?position=Стажёр", "Петров Пётр", "Иванов Иван"),
        ("/?q=Иванов", "Иванов Иван", "Петров Пётр"),
        ("/?q=Пётр", "Петров Пётр", "Иванов Иван"),
        ("/?q=иванов", "Иванов Иван", "Петров Пётр"),
    ]
    for url, present, absent in cases:
        body = client.get(url).get_data(as_text=True)
        assert present in body and absent not in body


def test_backup_export_import_and_reject_bad_file(client):
    page = client.get("/backup")
    assert "Резервное копирование" in page.get_data(as_text=True)
    exported = client.get("/backup/export")
    assert exported.status_code == 200
    assert exported.headers["Content-Disposition"].startswith("attachment")
    rejected = post(
        client, "/backup/import", {"dbfile": (io.BytesIO(b"not a db"), "bad.txt")},
        content_type="multipart/form-data", follow_redirects=True,
    )
    text = rejected.get_data(as_text=True)
    assert "Не удалось прочитать" in text or "не похож" in text
    accepted = post(
        client, "/backup/import", {"dbfile": (io.BytesIO(exported.data), "teambook_backup_1.db")},
        content_type="multipart/form-data", follow_redirects=True,
    )
    assert accepted.status_code == 200


@pytest.mark.parametrize("format_name, magic", [("xlsx", b"PK"), ("pdf", b"%PDF")])
def test_reports_download_for_all_and_employee(client, employee, format_name, magic):
    suffix = "" if format_name == "xlsx" else "&format=pdf"
    for url in (f"/report?year=2026{suffix}", f"/employee/{employee}/report?year=2026{suffix}"):
        response = client.get(url)
        assert response.status_code == 200
        assert response.data.startswith(magic)
        assert ".pdf" in response.headers["Content-Disposition"] if format_name == "pdf" else "teambook_" in response.headers["Content-Disposition"]


def test_board_columns_tasks_move_edit_archive_trash(app_env, client, employee):
    post(client, "/board/column/add", {"name": "В работе"})
    post(client, "/board/column/add", {"name": "Готово"})
    columns = db_rows(app_env, "SELECT * FROM kb_columns ORDER BY sort_order, id")
    assert len(columns) == 2
    assert not db_rows(app_env, "SELECT * FROM kb_columns WHERE kind='kanban' AND locked=1")
    post(client, f"/board/column/{columns[1]['id']}/rename", {"name": "Сделано"})
    post(client, "/board/task/add", {
        "title": "Задача А", "column_id": str(columns[0]["id"]),
        "employee_id": [str(employee)], "start_date": "2026-10-01",
        "due_date": "2026-10-10", "description": "описание А",
    })
    task = db_rows(app_env, "SELECT * FROM kb_tasks WHERE title='Задача А'")[0]
    assert task["column_id"] == columns[0]["id"]
    assert db_rows(app_env, "SELECT employee_id FROM kb_task_members WHERE task_id=?", (task["id"],))[0][0] == employee
    page = client.get("/board?month=10&year=2026").get_data(as_text=True)
    assert "Задача А" in page and "gcal-table" in page and "kb-zone" in page
    modal = client.get(f"/board/task/{task['id']}/card").get_data(as_text=True)
    checkbox = re.search(rf'<input[^>]*name="employee_id"[^>]*value="{employee}"[^>]*>', modal)
    assert checkbox and "checked" in checkbox.group(0) and "tm-employee-search" in modal
    post(client, f"/board/task/{task['id']}/edit", {
        "title": "Задача А (ред)", "column_id": str(columns[0]["id"]),
        "employee_id": [str(employee)], "start_date": "2026-11-02",
        "due_date": "2026-11-05", "description": "обновлено",
    })
    assert db_rows(app_env, "SELECT title FROM kb_tasks WHERE id=?", (task["id"],))[0][0] == "Задача А (ред)"
    response = post(client, f"/board/task/{task['id']}/trash")
    assert response.status_code == 204
    assert "Задача А (ред)" in client.get("/board/trash").get_data(as_text=True)
    post(client, f"/board/task/{task['id']}/restore")
    post(client, f"/board/task/{task['id']}/archive")
    assert "Задача А (ред)" in client.get("/board/archive").get_data(as_text=True)
    post(client, f"/board/task/{task['id']}/restore")
    row = db_rows(app_env, "SELECT archived_at, deleted_at FROM kb_tasks WHERE id=?", (task["id"],))[0]
    assert row["archived_at"] == row["deleted_at"] == ""


def test_board_column_reordering(app_env, client):
    for name in ("Первый", "Второй", "Третий"):
        post(client, "/board/column/add", {"name": name})
    rows = db_rows(app_env, "SELECT id, name FROM kb_columns ORDER BY sort_order, id")
    response = post(client, f"/board/column/{rows[-1]['id']}/move", {"before": str(rows[0]["id"])})
    assert response.status_code == 204
    assert db_rows(app_env, "SELECT name FROM kb_columns ORDER BY sort_order, id")[0][0] == "Третий"
    response = post(client, f"/board/column/{rows[-1]['id']}/move", {"before": ""})
    assert response.status_code == 204
    assert db_rows(app_env, "SELECT name FROM kb_columns ORDER BY sort_order, id")[-1][0] == "Третий"


def test_navigation_badges_dashboard_and_settings(app_env, client, employee):
    post(client, "/board/column/add", {"name": "Работа"})
    post(client, "/board/task/add", {
        "title": "Просроченная задача ЮЗ", "employee_id": [str(employee)],
        "due_date": "2000-01-01",
    })
    post(client, "/problem/add", {"employee_id": str(employee), "text": "Тестовая проблема сч"})
    home = client.get("/").get_data(as_text=True)
    assert re.search(r'Доска<span class="nav-badge">\d+</span>', home)
    assert re.search(r'Проблемы<span class="nav-badge">\d+</span>', home)
    assert "stats-row" in home and "открытых задач" in home and "просрочено" in home
    settings = client.get("/settings").get_data(as_text=True)
    assert "Telegram-бот" in settings and "Резервная копия" in settings and "tg_token" in settings
    post(client, "/settings/bot/save", {
        "tg_token": "123:TESTTOKEN", "tg_admin": "418650868", "tg_enabled": "1",
    })
    saved = {row["key"]: row["value"] for row in db_rows(app_env, "SELECT * FROM settings")}
    assert saved == {"tg_token": "123:TESTTOKEN", "tg_admin_id": "418650868", "tg_enabled": "1"}


def test_todo_backlog_matrix_archive_edit_order_and_delegate(app_env, client, employee):
    post(client, "/board/column/add", {"name": "Работа"})
    post(client, "/todo/add", {"title": "Туду-задача А"})
    post(client, "/todo/add", {"title": "Туду-задача Б"})
    todos = {row["title"]: row["id"] for row in db_rows(app_env, "SELECT * FROM todo_items")}
    page = client.get("/todo").get_data(as_text=True)
    assert all(text in page for text in ("Бэклог", "Важно · Срочно", "Не важно · Не срочно", "Сделать", "Запланировать"))
    response = post(client, f"/todo/{todos['Туду-задача А']}/move", {"status": "q_iu"})
    assert response.status_code == 204
    assert db_rows(app_env, "SELECT status FROM todo_items WHERE id=?", (todos["Туду-задача А"],))[0][0] == "q_iu"
    post(client, f"/todo/{todos['Туду-задача А']}/delegate")
    assert not db_rows(app_env, "SELECT * FROM todo_items WHERE id=?", (todos["Туду-задача А"],))
    assert db_rows(app_env, "SELECT * FROM kb_tasks WHERE title='Туду-задача А'")
    post(client, f"/todo/{todos['Туду-задача Б']}/done")
    row = db_rows(app_env, "SELECT status, done_date FROM todo_items WHERE id=?", (todos["Туду-задача Б"],))[0]
    assert row["status"] == "done" and row["done_date"] == datetime.date.today().isoformat()
    assert "Туду-задача Б" in client.get("/todo/archive").get_data(as_text=True)
    post(client, f"/todo/{todos['Туду-задача Б']}/move", {"status": "backlog"})
    post(client, f"/todo/{todos['Туду-задача Б']}/edit", {
        "title": "Туду-задача Б (ред)", "due_date": "2030-06-15", "tag": "люди",
    })
    page = client.get("/todo").get_data(as_text=True)
    assert "06-15" in page and "todo-tag" in page
    assert 'id="todoSearch"' in page and 'id="todoTagFilter"' in page
    assert "applyTpl" not in page and "Быстрое добавление" not in page
    assert 'id="themeToggle"' in page and "toggleTheme" in page


def test_employee_without_year_has_creation_prompt(app_env, client):
    post(client, "/employee/new", {"name": "Сидоров БезГодов", "position_id": "", "department_id": "", "salary": ""})
    employee_id = db_rows(app_env, "SELECT id FROM employees WHERE name='Сидоров БезГодов'")[0][0]
    body = client.get(f"/employee/{employee_id}").get_data(as_text=True)
    assert "нет созданных годов" in body
    assert "Полугодовые записи · 2026" not in body
