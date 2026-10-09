import sqlite3


def _create_board_fixture(app_env):
    with sqlite3.connect(app_env.DB_PATH) as db:
        column_id = db.execute(
            "INSERT INTO kb_columns(name, kind, sort_order) VALUES (?, ?, ?)",
            ("Работа", "kanban", 10),
        ).lastrowid
        db.execute(
            """INSERT INTO kb_tasks(column_id, title, description, due_date)
               VALUES (?, ?, ?, ?)""",
            (
                column_id,
                "Длинная задача",
                "Описание, которое должно быть визуально ограничено карточкой",
                "2030-11-23",
            ),
        )
        db.commit()


def test_board_renders_view_switcher_and_simplified_controls(app_env, client):
    _create_board_fixture(app_env)
    with sqlite3.connect(app_env.DB_PATH) as db:
        db.execute(
            "INSERT INTO kb_columns(name, kind, sort_order) VALUES (?, ?, ?)",
            ("Пустая", "kanban", 20),
        )
        db.commit()

    html = client.get("/board").get_data(as_text=True)

    assert 'class="board-view-switch"' in html
    assert 'data-board-view="kanban"' in html
    assert 'data-board-view="calendar"' in html
    assert 'data-board-view="both"' in html
    assert 'aria-pressed="true" data-board-view="kanban"' in html
    assert "Календарь сроков" in html
    assert 'class="filter-bar kb-filter-bar"' in html
    assert 'id="kbFilterReset"' in html
    assert 'id="kbFilterResult"' in html
    assert '<details class="action-menu kb-column-menu">' in html
    assert 'data-action="rename-column"' in html
    assert 'class="kb-menu-rename-form"' in html
    assert 'id="kbAddColumnToggle"' in html
    assert 'id="kbAddColumnForm"' in html
    assert "Пока нет задач" in html


def test_board_keeps_existing_server_routes_and_drag_contract(app_env, client):
    _create_board_fixture(app_env)

    html = client.get("/board").get_data(as_text=True)

    assert '/board/column/add' in html
    assert '/board/column/' in html and '/rename' in html and '/delete' in html
    assert '/board/task/add' in html
    assert 'ondragstart="dragTask(event)"' in html
    assert 'ondragstart="dragCol(event)"' in html
    assert "function dropOn" in html
    assert "function kbApplyFilters" in html
    assert "function kbSetView" in html
    assert "function kbColumnOrderKey" in html
    assert "getForm: function" in html
    assert "dirtyScope:" in html
    assert "localStorage.getItem('tb_board_view')" in html


def test_board_redesign_css_preserves_wrapping_and_adds_responsive_views():
    from pathlib import Path

    css = (Path(__file__).parents[1] / "static" / "style.css").read_text(
        encoding="utf-8"
    )

    assert ".kb-row { display: flex; flex-wrap: wrap" in css
    assert ".board-view-switch" in css
    assert '.board-page[data-board-view="kanban"] .gantt-panel' in css
    assert '.board-page[data-board-view="calendar"] .kanban-panel' in css
    assert ".kb-card-desc" in css and "-webkit-line-clamp" in css
    assert "overflow-x: auto" not in css[css.index(".kb-row"):css.index(".kb-col {")]