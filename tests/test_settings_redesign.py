def test_settings_are_grouped_and_token_is_masked(client):
    html = client.get("/settings").get_data(as_text=True)

    assert 'class="page-header settings-page-header"' in html
    assert 'data-settings-section="integrations"' in html
    assert 'data-settings-section="backup"' in html
    assert 'class="settings-section danger-zone"' in html
    assert 'type="password" id="tg_token"' in html
    assert 'id="tgTokenToggle"' in html
    assert 'aria-pressed="false"' in html
    assert "Включён в настройках" in html or "Отключён" in html
    assert 'id="restoreFileName"' in html
    assert 'class="btn danger" type="submit"' in html
    assert "Создадим резервную копию текущей базы" in html


def test_token_toggle_changes_only_input_type(client):
    html = client.get("/settings").get_data(as_text=True)

    assert "token.type = reveal ? 'text' : 'password'" in html
    assert "button.setAttribute('aria-pressed', reveal ? 'true' : 'false')" in html
    assert "token.value" not in html