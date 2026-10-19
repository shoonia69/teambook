from datetime import date


def test_birthday_migration_and_employee_form(app_env, client):
    with app_env.app.app_context():
        columns = {row[1] for row in app_env.get_db().execute("PRAGMA table_info(employees)")}
    assert "birth_date" in columns

    client.get("/employee/new")
    with client.session_transaction() as session:
        csrf = session["_csrf"]
    response = client.post(
        "/employee/new",
        data={"_csrf": csrf, "name": "Именинник", "hire_date": "", "birth_date": "1990-10-18"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Дата рождения" in html
    assert "18-10-1990" in html


def test_invalid_birthday_is_rejected_without_employee_creation(app_env, client):
    client.get("/employee/new")
    with client.session_transaction() as session:
        csrf = session["_csrf"]
    response = client.post(
        "/employee/new",
        data={"_csrf": csrf, "name": "Некорректный", "hire_date": "", "birth_date": "1990-02-30"},
    )
    assert response.status_code == 400
    with app_env.app.app_context():
        row = app_env.get_db().execute(
            "SELECT 1 FROM employees WHERE name='Некорректный'"
        ).fetchone()
    assert row is None


def test_topbar_has_separate_upcoming_birthdays_field(app_env, client, monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 12, 29)

    monkeypatch.setattr(app_env, "date", FixedDate)
    with app_env.app.app_context():
        db = app_env.get_db()
        db.executemany(
            "INSERT INTO employees(name, birth_date) VALUES (?, ?)",
            [
                ("Сегодня День", "1980-12-29"),
                ("Новый Год", "1991-01-01"),
                ("Вне горизонта", "1992-01-03"),
            ],
        )
        db.commit()
        notifications = app_env._notifications(db)

    assert [item["name"] for item in notifications["birthdays"]] == [
        "Сегодня День",
        "Новый Год",
    ]
    assert notifications["birthdays"][0]["days"] == 0
    assert notifications["birthdays"][1]["days"] == 3

    html = client.get("/").get_data(as_text=True)
    assert "Дни рождения · ближайшие 3 дня" in html
    assert "Сегодня День" in html
    assert "Новый Год" in html
    birthday_html = html[html.index('class="birthday-strip"'):html.index('class="user-area"')]
    notification_html = html[html.index('id="notifDrop"'):html.index('class="more"')]
    assert "Вне горизонта" not in birthday_html
    assert "Сегодня День" not in notification_html
    assert "Новый Год" not in notification_html
    assert notifications["total"] == 0


def test_topbar_birthday_field_has_empty_state(app_env, client):
    html = client.get("/").get_data(as_text=True)

    birthday_html = html[html.index('class="birthday-strip"'):html.index('class="user-area"')]
    assert "Дни рождения" in birthday_html
    assert "В ближайшие 3 дня дней рождения нет" in birthday_html


def test_birthday_field_mobile_layout_is_compact():
    css = open("static/style.css", encoding="utf-8").read()
    mobile = css[css.index("@media (max-width: 860px)"):]

    assert ".topbar { position: static;" in mobile
    assert ".birthday-strip { order: 3; width: 100%;" in mobile
