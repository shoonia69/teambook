# -*- coding: utf-8 -*-
"""Regression tests for SQLite integrity constraints and legacy normalization."""
import os
import re
import sqlite3
import sys
import tempfile

root = tempfile.mkdtemp()
os.environ["HR_DATA_DIR"] = root
os.environ["HR_PASSWORD"] = "x"

import app

app.init_db()
path = os.path.join(root, "hr_notes.db")
fail = []


def expect_rejected(db, sql, params, label):
    try:
        db.execute(sql, params)
        db.rollback()
        fail.append(label + ": invalid row accepted")
    except sqlite3.IntegrityError:
        db.rollback()


db = sqlite3.connect(path)
db.execute("PRAGMA foreign_keys=ON")
db.execute("INSERT INTO employees(name) VALUES ('Тест')")
eid = db.execute("SELECT id FROM employees").fetchone()[0]
db.execute("INSERT INTO kb_columns(name) VALUES ('Работа')")
cid = db.execute("SELECT id FROM kb_columns").fetchone()[0]
db.commit()

expect_rejected(db, "INSERT INTO todo_items(title,status) VALUES (?,?)", ("x", "bad"), "todo status")
expect_rejected(db, "INSERT INTO todo_items(title,status,done_date) VALUES (?,?,?)", ("x", "done", None), "todo null done_date")
expect_rejected(db, "INSERT INTO todo_items(title,due_date) VALUES (?,?)", ("x", "2026-02-30"), "todo date")
expect_rejected(db, "INSERT INTO todo_items(title,status,done_date) VALUES (?,?,?)", ("x", "backlog", "2026-01-01"), "todo done_date")
expect_rejected(db, "INSERT INTO year_records(employee_id,year,semester) VALUES (?,?,?)", (eid, 2026, "3H"), "semester")
expect_rejected(db, "INSERT INTO kb_tasks(column_id,title,start_date,due_date) VALUES (?,?,?,?)", (cid, "x", "2026-02-30", "2026-03-01"), "kanban date")
expect_rejected(db, "INSERT INTO kb_tasks(column_id,title,start_date) VALUES (?,?,?)", (cid, "x", None), "kanban null date")
expect_rejected(db, "INSERT INTO kb_tasks(column_id,title,start_date,due_date) VALUES (?,?,?,?)", (cid, "x", "2026-03-02", "2026-03-01"), "kanban range")
expect_rejected(db, "INSERT INTO kb_tasks(column_id,title) VALUES (?,?)", (999999, "x"), "kanban fk")
db.close()

# Legacy invalid-but-recoverable values are normalized before constraints are installed.
legacy = tempfile.mkdtemp()
legacy_path = os.path.join(legacy, "hr_notes.db")
os.environ["HR_DATA_DIR"] = legacy
app.DATA_DIR = legacy
app.DB_PATH = legacy_path
ldb = sqlite3.connect(legacy_path)
legacy_schema = app.SCHEMA.replace(" CHECK (semester IN ('1H', '2H'))", "")
for marker in (
    " CONSTRAINT ck_year_records_semester",
    " CONSTRAINT ck_kb_tasks_start_date",
    " CONSTRAINT ck_kb_tasks_due_date",
    " CONSTRAINT ck_todo_status",
    " CONSTRAINT ck_todo_done_date",
    " CONSTRAINT ck_todo_due_date",
    "    CONSTRAINT ck_kb_tasks_date_order\n",
    "    CONSTRAINT ck_todo_done_state\n",
):
    legacy_schema = legacy_schema.replace(marker, "")
legacy_schema = legacy_schema.replace(
    "\n                  CHECK (status IN ('backlog','q_iu','q_in','q_nu','q_nn','done'))", "")
legacy_schema = legacy_schema.replace(" CHECK (done_date = '' OR date(done_date) = done_date)", "")
legacy_schema = legacy_schema.replace(" CHECK (due_date = '' OR date(due_date) = due_date)", "")
legacy_schema = legacy_schema.replace(" CHECK (start_date = '' OR date(start_date) = start_date)", "")
legacy_schema = legacy_schema.replace(
    ",\n    CHECK (start_date = '' OR due_date = '' OR start_date <= due_date)", "")
legacy_schema = legacy_schema.replace(
    ",\n    CHECK ((status = 'done' AND done_date != '') OR (status != 'done' AND done_date = ''))", "")
ldb.executescript(legacy_schema)
ldb.execute("INSERT INTO employees(name) VALUES ('Legacy')")
le = ldb.execute("SELECT id FROM employees").fetchone()[0]
ldb.execute("INSERT INTO year_records(employee_id,year,semester) VALUES (?,?,?)", (le, 2026, "3H"))
ldb.execute("ALTER TABLE year_records ADD COLUMN space_owner INTEGER")
ldb.execute("UPDATE year_records SET space_owner=42")
ldb.execute("INSERT INTO todo_items(title,status,done_date,due_date) VALUES (?,?,?,?)", ("legacy", "today", "2026-01-01", "broken"))
ldb.execute("INSERT INTO kb_columns(name) VALUES ('Legacy')")
lc = ldb.execute("SELECT id FROM kb_columns").fetchone()[0]
ldb.execute("INSERT INTO kb_tasks(column_id,title,start_date,due_date) VALUES (?,?,?,?)", (lc, "legacy", "broken", "2026-01-01"))
ldb.commit(); ldb.close()
app.init_db(); app.init_db()
ldb = sqlite3.connect(legacy_path)
row = ldb.execute("SELECT status,done_date,due_date FROM todo_items").fetchone()
if row != ("q_iu", "", ""):
    fail.append("legacy todo not normalized: %r" % (row,))
yr = ldb.execute("SELECT semester FROM year_records").fetchone()[0]
if yr != "1H":
    fail.append("legacy semester not normalized: %r" % yr)
kt = ldb.execute("SELECT start_date,due_date FROM kb_tasks").fetchone()
if kt != ("", "2026-01-01"):
    fail.append("legacy kanban dates not normalized: %r" % (kt,))
if ldb.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    fail.append("integrity_check failed")
if ldb.execute("PRAGMA foreign_key_check").fetchall():
    fail.append("foreign_key_check failed")
ldb.close()

# A decoy CHECK must neither suppress migration nor satisfy restore validation.
fake = sqlite3.connect(":memory:")
fake.executescript(legacy_schema.replace(
    "year INTEGER NOT NULL,", "year INTEGER NOT NULL CHECK (year > 0),").replace(
    "sort_order    INTEGER NOT NULL DEFAULT 0,",
    "sort_order    INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),"))
errs = app._validate_db_schema(fake)
if not any("CHECK-контракт" in e for e in errs):
    fail.append("restore validator accepted decoy CHECK constraints")
fake.close()

forged = sqlite3.connect(":memory:")
forged_schema = app.SCHEMA
for expression in app.REQUIRED_CHECKS["todo_items"].values():
    forged_schema = forged_schema.replace("CHECK (" + expression + ")", "CHECK (1)")
# Replace directly against formatted source expressions to model same-name permissive checks.
forged_schema = re.sub(
    r"(CONSTRAINT ck_todo_[a-z_]+\s+CHECK\s*)\((?:[^()]|\([^()]*\))*\)",
    r"\1(1)", forged_schema)
forged.executescript(forged_schema)
errs = app._validate_db_schema(forged)
if not any("неверные CHECK-контракты" in e for e in errs):
    fail.append("restore validator accepted forged named CHECK constraints")
forged.close()

duplicate = sqlite3.connect(":memory:")
duplicate_schema = app.SCHEMA.replace(
    "status        TEXT NOT NULL DEFAULT 'backlog' CONSTRAINT ck_todo_status",
    "status        TEXT NOT NULL DEFAULT 'backlog' "
    "CONSTRAINT ck_todo_status CHECK (1) CONSTRAINT ck_todo_status",
    1,
)
duplicate.executescript(duplicate_schema)
errs = app._validate_db_schema(duplicate)
if not any("неверные CHECK-контракты" in e for e in errs):
    fail.append("restore validator accepted duplicate named CHECK constraints")
duplicate.close()

if fail:
    print("FAIL:", fail)
    sys.exit(1)
print("SCHEMA CONSTRAINTS OK")
