from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_local_svg_sprite_contains_required_interface_icons():
    sprite = (ROOT / "static" / "icons.svg").read_text(encoding="utf-8")

    for name in (
        "search", "plus", "more", "edit", "check", "archive", "trash",
        "calendar", "download", "upload", "settings", "close", "theme", "drag",
    ):
        assert f'id="icon-{name}"' in sprite


def test_shared_icon_macro_and_styles_exist():
    macro = (ROOT / "templates" / "_icons.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert "filename='icons.svg'" in macro
    assert "#icon-{{ name }}" in macro
    assert 'aria-hidden="true"' in macro
    assert ".ui-icon" in css


def test_primary_templates_use_local_icons_instead_of_action_emoji():
    for name in ("base.html", "board.html", "todo.html", "index.html", "settings.html", "catalogs.html"):
        source = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "{% from \"_icons.html\" import icon %}" in source

    catalogs = (ROOT / "templates" / "catalogs.html").read_text(encoding="utf-8")
    assert "💾" not in catalogs and "✕" not in catalogs
    assert 'aria-label="Переименовать' in catalogs
    assert 'aria-label="Удалить' in catalogs


def test_todo_search_uses_shared_filter_bar_components():
    todo = (ROOT / "templates" / "todo.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert 'class="grow search-input-wrap"' in todo
    assert "{{ icon('search') }}" in todo
    assert 'class="filter-bar-result"' in todo
    assert ".search-input-wrap:focus-within" in css
    assert ".todo-filterbar" not in css