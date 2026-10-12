from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_long_content_and_mobile_touch_targets_are_hardened():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    for selector in (
        ".kb-col-name",
        ".kb-card-title",
        ".todo-title",
        ".body-text",
        ".employee-facts strong",
        ".notif-txt",
    ):
        assert selector in css
    assert "overflow-wrap: anywhere" in css
    assert "max-width: calc(100vw - 16px)" in css
    assert "min-block-size: 44px" in css
    assert ".restore-file-row input[type=\"file\"]" in css


def test_theme_toggle_exposes_current_state(client):
    html = client.get("/").get_data(as_text=True)

    assert 'id="themeToggle"' in html
    assert 'aria-pressed=' in html
    assert "setAttribute('aria-label'" in html
    assert "setAttribute('aria-pressed'" in html


def test_employee_notes_are_owned_by_development_panel(app_env, client):
    with app_env.app.app_context():
        db = app_env.get_db()
        db.execute("INSERT INTO employees(name) VALUES (?)", ("Проверка вкладок",))
        eid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit()
    html = client.get(f"/employee/{eid}").get_data(as_text=True)
    development_start = html.index('data-employee-panel="development"')
    development_end = html.index('data-employee-panel="problems"')
    notes = html.index('data-acc="notes"')
    assert development_start < notes < development_end


def test_todo_menus_render_above_cards():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert ".todo-item:has(.todo-menu[open])" in css
    assert "overflow: visible" in css
    assert ".todo-menu[open]" in css
    assert "z-index: 80" in css