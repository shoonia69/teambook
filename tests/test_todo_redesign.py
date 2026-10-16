def test_todo_uses_compact_cards_menus_and_consistent_hotkeys(app_env, client):
    with app_env.app.app_context():
        db = app_env.get_db()
        db.execute(
            "INSERT INTO todo_items(title, status, sort_order) VALUES (?,?,?)",
            ("Проверить карточку", "backlog", 10),
        )
        db.commit()
    html = client.get("/todo").get_data(as_text=True)

    assert 'class="page-header todo-page-header"' in html
    assert 'class="filter-bar todo-filter-bar"' in html
    assert 'placeholder="Поиск по задачам..."' in html
    assert 'class="grow search-input-wrap"' in html
    assert 'id="todoFilterResult"' in html
    assert 'data-todo-count="backlog"' in html
    assert 'data-todo-card' in html
    assert 'data-todo-open' in html
    assert '<details class="action-menu todo-menu"' in html
    assert "function todoCardClick" in html
    assert "document.getElementById('todoSearch').focus()" in html
    assert "document.getElementById('todoNewTitle').focus()" in html
    assert "Перетащите сюда задачу из бэклога" in html


def test_todo_filter_updates_all_counts(client):
    html = client.get("/todo").get_data(as_text=True)

    assert "document.querySelectorAll('[data-todo-card]')" in html
    assert "document.querySelectorAll('[data-todo-container]')" in html
    assert "document.getElementById('todoFilterResult')" in html
    assert "result.textContent" in html


def test_todo_archive_filters_by_inclusive_period(app_env, client):
    with app_env.app.app_context():
        db = app_env.get_db()
        db.executemany(
            "INSERT INTO todo_items(title, status, done_date) VALUES (?, 'done', ?)",
            [
                ("До периода", "2026-01-09"),
                ("Начало периода", "2026-01-10"),
                ("Конец периода", "2026-01-12"),
                ("После периода", "2026-01-13"),
            ],
        )
        db.commit()

    response = client.get("/todo/archive?date_from=2026-01-10&date_to=2026-01-12")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Начало периода" in html
    assert "Конец периода" in html
    assert "До периода" not in html
    assert "После периода" not in html
    assert 'name="date_from" value="2026-01-10"' in html
    assert 'name="date_to" value="2026-01-12"' in html


def test_todo_archive_rejects_reversed_period(client):
    response = client.get("/todo/archive?date_from=2026-01-12&date_to=2026-01-10")

    assert response.status_code == 400