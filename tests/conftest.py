import importlib
import sys

import pytest


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HR_PASSWORD", "test-password")
    monkeypatch.setenv("HR_SECRET_KEY", "test-secret-key")
    sys.modules.pop("app", None)
    app_module = importlib.import_module("app")
    app_module.app.config.update(TESTING=True)
    app_module.init_db()
    yield app_module
    with app_module.app.app_context():
        app_module.close_db(None)
    sys.modules.pop("app", None)


@pytest.fixture
def client(app_env):
    client = app_env.app.test_client()
    response = client.post("/login", data={"password": "test-password"})
    assert response.status_code == 302
    return client
