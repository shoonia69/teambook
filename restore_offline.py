# -*- coding: utf-8 -*-
"""Offline restore helper: выполняется PID1 без активных app writers."""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

DATA = os.environ.get("HR_DATA_DIR", "/app/data")
REQ = os.path.join(DATA, ".restore-request.json")
LOCK = os.path.join(DATA, ".maintenance.lock")
FATAL = os.path.join(DATA, ".restore-fatal")
CURRENT = os.path.join(DATA, "hr_notes.db")
NAME_RE = re.compile(r"^\.restore-staged-[0-9a-f]{16}\.db$")


def _fsync_dir(path):
    if os.name == "nt": return
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def _remove_sidecars(path):
    for suffix in ("-wal", "-shm"):
        try: os.remove(path + suffix)
        except FileNotFoundError: pass


def _quarantine(reason, staged=None, fatal=False):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    rejected = os.path.join(DATA, ".restore-rejected-" + stamp + ".json")
    try: os.replace(REQ, rejected)
    except FileNotFoundError: pass
    if staged and os.path.exists(staged):
        try: os.replace(staged, rejected + ".db")
        except OSError: pass
    with open(rejected + ".error", "w", encoding="utf-8") as fh:
        fh.write(reason + "\n"); fh.flush(); os.fsync(fh.fileno())
    if fatal:
        with open(FATAL, "w", encoding="utf-8") as fh:
            fh.write(reason + "\n"); fh.flush(); os.fsync(fh.fileno())
    _fsync_dir(DATA)


def _validate_path(raw):
    if not isinstance(raw, str): raise ValueError("staged path must be a string")
    real_data = os.path.realpath(DATA); real = os.path.realpath(raw)
    if os.path.dirname(real) != real_data or not NAME_RE.fullmatch(os.path.basename(real)):
        raise ValueError("invalid staged path")
    st = os.lstat(raw)
    if os.path.islink(raw) or not os.path.isfile(raw): raise ValueError("staged file is not regular")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid(): raise ValueError("invalid staged owner")
    if os.name != "nt" and st.st_mode & 0o022:
        raise ValueError("staged file is group/world writable")
    return real, (st.st_dev, st.st_ino)


def _validate_db(path, expected_inode=None):
    if expected_inode:
        st = os.lstat(path)
        if (st.st_dev, st.st_ino) != expected_inode: raise ValueError("staged file changed")
    import app as appmod
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        errors = appmod._validate_db_schema(con)
        if errors: raise ValueError("; ".join(errors))
    finally: con.close()


def activate():
    if not os.path.exists(REQ): return 0
    staged = None; swapped = False; rollback_ok = False; backup = None
    try:
        with open(REQ, encoding="utf-8") as fh: payload = json.load(fh)
        staged, inode = _validate_path(payload.get("staged"))
        _validate_db(staged, inode)
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, str(os.getpid()).encode()); os.fsync(fd); os.close(fd)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup = CURRENT + ".pre_restore_" + stamp + ".bak"
        if os.path.exists(CURRENT):
            src = sqlite3.connect(CURRENT); dst = sqlite3.connect(backup)
            try: src.backup(dst)
            finally: src.close(); dst.close()
            fd = os.open(backup, os.O_RDWR)
            try: os.fsync(fd)
            finally: os.close(fd)
        _validate_db(staged, inode)
        _remove_sidecars(CURRENT)
        os.replace(staged, CURRENT); swapped = True
        try:
            _validate_db(CURRENT)
        except Exception as activation_exc:
            _remove_sidecars(CURRENT)
            if not backup or not os.path.exists(backup): raise
            try:
                os.replace(backup, CURRENT)
                _remove_sidecars(CURRENT)
                _validate_db(CURRENT)
                fd = os.open(CURRENT, os.O_RDWR)
                try: os.fsync(fd)
                finally: os.close(fd)
                _fsync_dir(DATA)
                rollback_ok = True
            except Exception as rollback_exc:
                raise RuntimeError("rollback failed: %s" % rollback_exc) from activation_exc
            raise activation_exc
        fd = os.open(CURRENT, os.O_RDWR)
        try: os.fsync(fd)
        finally: os.close(fd)
        os.remove(REQ); _fsync_dir(DATA)
        return 75
    except Exception as exc:
        fatal = swapped and not rollback_ok
        _quarantine(str(exc), staged=staged, fatal=fatal)
        return 76 if fatal else 0
    finally:
        try: os.remove(LOCK)
        except FileNotFoundError: pass


if __name__ == "__main__":
    sys.exit(activate())
