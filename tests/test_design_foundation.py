from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_design_tokens_focus_and_reduced_motion_are_defined():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    for token in (
        "--space-1:",
        "--space-2:",
        "--space-3:",
        "--space-4:",
        "--space-5:",
        "--radius-sm:",
        "--radius-md:",
        "--radius-lg:",
        "--shadow-sm:",
        "--shadow-md:",
        "--focus-ring:",
        "--warning:",
    ):
        assert token in css

    assert ":focus-visible" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_foundation_components_and_shared_ui_script_are_available(client):
    html = client.get("/").get_data(as_text=True)
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    ui_js = ROOT / "static" / "ui.js"

    assert 'class="page-header"' in html
    assert 'class="page-title"' in html
    assert 'class="filter-bar"' in html
    assert 'static/ui.js' in html
    assert "defer" in html
    assert ui_js.exists()
    ui_source = ui_js.read_text(encoding="utf-8")
    assert "initActionMenus" in ui_source
    assert 'trigger.setAttribute("aria-controls", panel.id)' in ui_source
    assert 'trigger.setAttribute("aria-expanded", "false")' in ui_source
    assert "panel.hidden = true" in ui_source

    for selector in (
        ".page-header",
        ".page-title",
        ".section-title",
        ".card-title",
        ".body-text",
        ".meta-text",
        ".helper-text",
        ".filter-bar",
        ".action-menu",
        ".empty-state",
    ):
        assert selector in css


def test_modal_manager_tracks_supplied_scope_and_resets_on_form_submission(client):
    html = client.get("/").get_data(as_text=True)

    assert "var dirtyScope = opts.dirtyScope || overlay" in html
    assert "dirtyScope.addEventListener('input'" in html
    assert "dirtyScope.addEventListener('change'" in html
    assert "dirtyScope.addEventListener('submit'" in html
    assert "var form = opts.getForm ? opts.getForm() : e.target" in html
    assert "form.addEventListener('formdata', function () { dirty = false; }" in html