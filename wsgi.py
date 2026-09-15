# -*- coding: utf-8 -*-
"""Точка входа для gunicorn: инициализирует БД и отдаёт приложение."""
from app import app, init_db

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(__import__("os").environ.get("PORT", 5000)))