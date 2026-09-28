# -*- coding: utf-8 -*-
"""
Супервизор Telegram-бота TeamBook.

Живёт всё время, раз в 3 секунды читает настройки из БД (settings).
Если включено (tg_enabled=1) и задан токен — гарантирует работу воркера
bot_worker.py с актуальными токеном/админом. Если в настройках что-то
изменилось или включение/выключение — перезапускает воркер.

Так «кнопка старта» из веб-настроек работает без рестарта контейнера.
"""
import os
import subprocess
import sys
import sqlite3
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("teambook-bot-supervisor")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.environ.get("HR_DATA_DIR", BASE_DIR + "/data"), "hr_notes.db")
WORKER = os.path.join(BASE_DIR, "bot_worker.py")


def read_settings():
    try:
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}
        c.close()
        return {
            "enabled": rows.get("tg_enabled", "0") == "1",
            "token": rows.get("tg_token", ""),
            "admin": rows.get("tg_admin_id", ""),
        }
    except Exception as e:
        log.warning("Не удалось прочитать настройки: %s", e)
        return {"enabled": False, "token": "", "admin": ""}


def worker_signature(cfg):
    """Ключ идентичности текущего воркера."""
    return (cfg["token"], cfg["admin"])


def main():
    worker = None
    last_sig = None

    while True:
        cfg = read_settings()
        should_run = cfg["enabled"] and bool(cfg["token"])
        sig = worker_signature(cfg) if should_run else None

        if worker is not None:
            alive = worker.poll() is None
            if not alive:
                log.info("Воркер завершился (rc=%s)", worker.returncode)
                worker = None
                last_sig = None

        need = sig != last_sig
        if need:
            if worker is not None:
                log.info("Конфигурация изменилась — останавливаю воркер")
                worker.terminate()
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.kill()
                worker = None
            if should_run:
                env = dict(os.environ)
                env["TG_TOKEN"] = cfg["token"]
                env["TG_ADMIN"] = cfg["admin"]
                env["TG_DB"] = DB_PATH
                os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
                logfile = open(os.path.join(os.path.dirname(DB_PATH), "teambot.log"), "ab")
                log.info("Запускаю воркер бота (admin=%s)", cfg["admin"])
                worker = subprocess.Popen(
                    [sys.executable, WORKER], env=env,
                    stdout=logfile, stderr=subprocess.STDOUT)
                last_sig = sig
            else:
                log.info("Бот отключён или токен не задан — не запускаю")
                last_sig = None

        time.sleep(3)


if __name__ == "__main__":
    main()