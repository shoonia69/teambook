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
    assert 'placeholder="Поиск задач — клавиша /"' in html
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