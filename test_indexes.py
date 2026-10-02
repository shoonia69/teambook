# -*- coding: utf-8 -*-
"""Регрессии прикладных SQLite-индексов TeamBook."""
import os
import sqlite3
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["HR_DATA_DIR"] = _tmp
os.environ["HR_PASSWORD"] = "index-test"

import app as appmod

failures = []


def check(label, condition, extra=""):
    print(("PASS" if condition else "FAIL"), "-", label, extra)
    if not condition:
        failures.append(label)


appmod.init_db()
db = sqlite3.connect(appmod.DB_PATH)

expected = {
    "idx_login_failures_ip_time",
    "idx_employee_history_employee_date",
    "idx_meetings_employee_date",
    "idx_problems_employee_id",
    "idx_year_records_year",
    "idx_kb_columns_kind_order",
    "idx_kb_tasks_live",
    "idx_kb_tasks_live_due",
    "idx_kb_tasks_deleted",
    "idx_kb_tasks_archived",

    "idx_todo_open_due",
    "idx_todo_status_order",
    "idx_todo_done_date",
}
actual = {r[0] for r in db.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
)}
check("созданы все прикладные индексы", expected <= actual,
      "missing=%s" % sorted(expected - actual))

plans = {
    "login limiter": ("SELECT COUNT(*) FROM login_failures WHERE ip=? AND failed_at>=?", ("ip", 0), "idx_login_failures_ip_time"),
    "основной список канбана": ("SELECT t.* FROM kb_tasks t WHERE t.archived_at='' AND t.deleted_at='' ORDER BY t.id DESC", (), "idx_kb_tasks_live"),
    "уведомления канбана": ("SELECT t.id,t.title,t.due_date,t.archived_at,t.deleted_at,c.name FROM kb_tasks t LEFT JOIN kb_columns c ON c.id=t.column_id WHERE t.archived_at='' AND t.deleted_at='' AND t.due_date!='' AND t.due_date<=? ORDER BY t.due_date", ("2099-01-01",), "idx_kb_tasks_live_due"),
    "корзина канбана": ("SELECT t.*,(SELECT COUNT(*) FROM kb_task_members m WHERE m.task_id=t.id) n FROM kb_tasks t WHERE t.deleted_at!='' ORDER BY t.deleted_at DESC,t.id DESC", (), "idx_kb_tasks_deleted"),
    "архив канбана": ("SELECT t.*,(SELECT COUNT(*) FROM kb_task_members m WHERE m.task_id=t.id) n FROM kb_tasks t WHERE t.archived_at!='' AND t.deleted_at='' ORDER BY t.archived_at DESC,t.id DESC", (), "idx_kb_tasks_archived"),
    "уведомления todo": ("SELECT id,title,due_date,'' AS col_name,'' AS emp FROM todo_items WHERE status!='done' AND due_date!='' AND due_date<=? ORDER BY due_date", ("2099-01-01",), "idx_todo_open_due"),
    "архив todo": ("SELECT * FROM todo_items WHERE status='done' AND done_date=? ORDER BY id DESC", ("2026-01-01",), "idx_todo_done_date"),
    "встречи сотрудника": ("SELECT * FROM meetings WHERE employee_id=? ORDER BY date DESC,id DESC", (1,), "idx_meetings_employee_date"),
    "проблемы сотрудника": ("SELECT * FROM problems WHERE employee_id=? ORDER BY id DESC", (1,), "idx_problems_employee_id"),
    "история сотрудника": ("SELECT * FROM employee_history WHERE employee_id=? ORDER BY change_date DESC,id DESC", (1,), "idx_employee_history_employee_date"),
    "записи года": ("SELECT COUNT(*) FROM year_records WHERE year=?", (2026,), "idx_year_records_year"),
    "сортировка колонок": ("SELECT * FROM kb_columns WHERE kind='kanban' ORDER BY sort_order,id", (), "idx_kb_columns_kind_order"),
    "порядок todo": ("SELECT * FROM todo_items WHERE status=? ORDER BY sort_order,id", ("backlog",), "idx_todo_status_order"),
}
for label, (sql, params, name) in plans.items():
    detail = " | ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql, params))
    check(label + " использует индекс", name in detail, detail)

db.close()
print()
if failures:
    print("ИТОГ: ПРОВАЛЫ ->", failures)
    raise SystemExit(1)
print("ИТОГ: INDEX PACK OK")
