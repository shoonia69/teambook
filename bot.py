# -*- coding: utf-8 -*-
"""Supervisor Telegram worker с graceful shutdown и backoff."""
import logging
import os
import random
import signal
import sqlite3
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("teambook-bot-supervisor")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("HR_DATA_DIR", BASE_DIR + "/data")
DB_PATH = os.path.join(DATA_DIR, "hr_notes.db")
MAINTENANCE_LOCK = os.path.join(DATA_DIR, ".maintenance.lock")
WORKER = os.path.join(BASE_DIR, "bot_worker.py")
BACKOFF_MAX = 300.0
FAST_FAILURE_SECONDS = 30.0
POLL_SECONDS = 3.0
STOP = False


def _signal_stop(signum, frame):
    global STOP
    STOP = True


def read_settings():
    try:
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 10000")
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
    return (cfg["token"], cfg["admin"])


def stop_worker(worker, logfile):
    if worker is not None and worker.poll() is None:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=5)
    if logfile is not None:
        logfile.close()
    return None, None


def main():
    global STOP
    signal.signal(signal.SIGTERM, _signal_stop)
    signal.signal(signal.SIGINT, _signal_stop)
    worker = None
    logfile = None
    last_sig = None
    started_at = 0.0
    failures = 0
    retry_at = 0.0

    try:
        while not STOP:
            maintenance = os.path.exists(MAINTENANCE_LOCK)
            cfg = read_settings() if not maintenance else {"enabled": False, "token": "", "admin": ""}
            should_run = cfg["enabled"] and bool(cfg["token"]) and not maintenance
            sig = worker_signature(cfg) if should_run else None

            if worker is not None and worker.poll() is not None:
                runtime = time.monotonic() - started_at
                log.warning("Воркер завершился (rc=%s, runtime=%.1fs)", worker.returncode, runtime)
                worker, logfile = stop_worker(worker, logfile)
                if runtime < FAST_FAILURE_SECONDS:
                    failures += 1
                else:
                    failures = 1
                backoff = min(BACKOFF_MAX, 2 ** min(failures, 8))
                retry_at = time.monotonic() + backoff + random.uniform(0, min(3.0, backoff / 4))

            config_changed = sig != last_sig
            if config_changed:
                worker, logfile = stop_worker(worker, logfile)
                failures = 0
                retry_at = 0.0
                last_sig = sig

            if maintenance and worker is not None:
                log.info("Maintenance lock — останавливаю Telegram worker")
                worker, logfile = stop_worker(worker, logfile)

            if should_run and worker is None and time.monotonic() >= retry_at:
                env = dict(os.environ)
                env["TG_TOKEN"] = cfg["token"]
                env["TG_ADMIN"] = cfg["admin"]
                env["TG_DB"] = DB_PATH
                os.makedirs(DATA_DIR, exist_ok=True)
                logfile = open(os.path.join(DATA_DIR, "teambot.log"), "ab")
                log.info("Запускаю воркер бота (admin=%s, failures=%s)", cfg["admin"], failures)
                worker = subprocess.Popen(
                    [sys.executable, WORKER], env=env,
                    stdout=logfile, stderr=subprocess.STDOUT)
                started_at = time.monotonic()

            time.sleep(POLL_SECONDS)
    finally:
        stop_worker(worker, logfile)


if __name__ == "__main__":
    main()
