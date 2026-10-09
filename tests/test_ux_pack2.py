# -*- coding: utf-8 -*-
"""Structural regressions for UX pack 2."""
from pathlib import Path

import pytest


@pytest.fixture
def ux_pages(app_env, client):
    with app_env.app.app_context():
        db = app_env.get_db()
        position_id = db.execute(
            "INSERT INTO positions(name) VALUES (?)", ("Инженер",)
        ).lastrowid
        department_id = db.execute(
            "INSERT INTO departments(name) VALUES (?)", ("ТП",)
        ).lastrowid
        db.execute(
            "INSERT INTO employees(name, position_id, department_id) VALUES (?, ?, ?)",
            ("Иванов", position_id, department_id),
        )
        column_id = db.execute(
            "INSERT INTO kb_columns(name, kind, sort_order) VALUES (?, ?, ?)",
            ("Бэклог", "kanban", 1),
        ).lastrowid
        db.execute(
            "INSERT INTO kb_tasks(title, column_id) VALUES (?, ?)",
            ("Задача", column_id),
        )
        db.execute(
            "INSERT INTO todo_items(title, status) VALUES (?, ?)",
            ("Тодо", "backlog"),
        )
        db.commit()

    responses = {
        "head": client.get("/todo"),
        "board": client.get("/board"),
        "todo": client.get("/todo"),
        "index": client.get("/"),
    }
    for response in responses.values():
        assert response.status_code == 200

    return {
        name: response.get_data(as_text=True)
        for name, response in responses.items()
    }


@pytest.fixture
def base_css():
    return (Path(__file__).parents[1] / "static" / "style.css").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("marker", "contract"),
    [
        ("window.initModal", "shared modal manager"),
        ("setAttribute('role', 'dialog')", "dialog role"),
        ("setAttribute('aria-modal', 'true')", "modal semantics"),
        ("function trap(e)", "Tab focus trap"),
        ("overlay.style.display === 'flex'", "Escape only for open modal"),
        ("Есть несохранённые изменения", "dirty-state warning"),
        ("lastTrigger.focus()", "trigger focus restoration"),
        ("!overlay.contains(document.activeElement)", "escaped-focus recovery"),
        ("el.inert = true", "inert background"),
    ],
)
def test_shared_modal_manager_accessibility(ux_pages, marker, contract):
    assert marker in ux_pages["head"], contract


@pytest.mark.parametrize(
    ("page", "marker", "contract"),
    [
        ("todo", "todoModal", "todo uses modal manager"),
        ("todo", "todoModal.open(returnFocus)", "todo restores trigger focus"),
        ("board", 'role="button" tabindex="0"', "board cards are keyboard focusable"),
        ("board", "openCardKey(event", "board cards support Enter/Space"),
        ("board", "cardModal.open(returnFocus)", "board restores card focus"),
    ],
)
def test_page_modal_integration(ux_pages, page, marker, contract):
    assert marker in ux_pages[page], contract


@pytest.mark.parametrize(
    ("marker", "contract"),
    [
        ("if (opts.checkOk && !r.ok) return Promise.reject", "non-2xx rejection"),
        ("if (opts.onError) opts.onError(err)", "handled async failure"),
        ("aria-busy", "pending-state accessibility"),
        ("window.tbToast", "toast helper"),
        ("setAttribute('role', isErr ? 'alert' : 'status')", "toast live-region role"),
        ('id="tb-toast"', "toast container"),
    ],
)
def test_shared_async_post_contract(ux_pages, marker, contract):
    assert marker in ux_pages["head"], contract


@pytest.mark.parametrize("page", ["board", "todo"])
def test_interactive_pages_use_shared_post_helper(ux_pages, page):
    assert "window.postForm" in ux_pages[page]


@pytest.mark.parametrize(
    ("marker", "contract"),
    [
        ("window.hotkeyGuard", "hotkey guard"),
        ("ctrlKey || e.altKey || e.metaKey", "modifier-key guard"),
        ("isComposing", "IME composition guard"),
        ("isEditableTarget", "editable-target guard"),
    ],
)
def test_shared_hotkey_safety(ux_pages, marker, contract):
    assert marker in ux_pages["head"], contract


def test_todo_hotkey_uses_shared_guard(ux_pages):
    assert "window.hotkeyGuard(e)" in ux_pages["todo"]


@pytest.mark.parametrize("css_class", [".page-head", ".compact-filter", ".form-actions"])
def test_shared_form_and_filter_css(base_css, css_class):
    assert css_class in base_css


@pytest.mark.parametrize(
    "marker",
    ['class="filter-bar"', 'name="department"', 'name="position"'],
)
def test_index_uses_compact_combined_filters(ux_pages, marker):
    assert marker in ux_pages["index"]


@pytest.mark.parametrize(
    ("marker", "contract"),
    [
        ("@media (max-width: 420px)", "narrow-screen breakpoint"),
        (".modal-card { max-width: 100%", "non-overflowing narrow modal"),
    ],
)
def test_narrow_viewport_css(base_css, marker, contract):
    assert marker in base_css, contract
