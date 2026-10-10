def test_employee_page_has_summary_actions_and_accessible_tabs(app_env, client):
    with app_env.app.app_context():
        db = app_env.get_db()
        db.execute("INSERT INTO employees(name, salary, hire_date) VALUES (?,?,?)", ("Тест Сотрудник", "100 000 ₽", "2024-03-15"))
        eid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO problems(employee_id, text) VALUES (?,?)", (eid, "Проверить нагрузку"))
        db.execute("INSERT INTO meetings(employee_id, date, summary) VALUES (?,?,?)", (eid, "2026-09-01", "Итоги встречи"))
        db.execute("INSERT INTO year_records(employee_id, year, semester) VALUES (?,?,?)", (eid, 2026, "1H"))
        db.commit()

    html = client.get(f"/employee/{eid}").get_data(as_text=True)
    assert 'class="employee-summary"' in html
    assert "Активные проблемы" in html and "Последняя 1-на-1" in html
    assert '<details class="action-menu employee-actions"' in html
    assert "Excel" in html and "PDF" in html
    assert 'role="tablist"' in html
    for tab in ("overview", "development", "meetings", "history", "problems"):
        assert f'data-employee-tab="{tab}"' in html
        assert f'data-employee-panel="{tab}"' in html
        assert f'id="employee-tab-{tab}"' in html
        assert f'id="employee-panel-{tab}"' in html
        assert f'aria-controls="employee-panel-{tab}"' in html
        assert f'aria-labelledby="employee-tab-{tab}"' in html
    assert html.count('data-employee-panel="overview"') == 1
    assert html.count("Скачать Excel") == 1
    assert "⬇ Excel" not in html and "⬇ PDF" not in html
    assert 'class="action-menu year-actions"' in html
    assert "[data-employee-panel=\"development\"] .semester-block form" in html
    assert "window.location.hash" in html
    assert "beforeunload" in html


def test_employee_tabs_own_their_real_content(app_env, client):
    with app_env.app.app_context():
        db = app_env.get_db()
        db.execute("INSERT INTO employees(name) VALUES (?)", ("Вкладки Сотрудник",))
        eid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit()
    html = client.get(f"/employee/{eid}").get_data(as_text=True)
    assert html.index('data-employee-panel="history"') < html.index("История изменения должности")
    assert html.index('data-employee-panel="development"') < html.index("Создать год:")
    assert html.index('data-employee-panel="problems"') < html.index("Проблемы в работе")
    assert html.index('data-employee-panel="meetings"') < html.index("Встречи 1-на-1")