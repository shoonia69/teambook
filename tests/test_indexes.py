import sqlite3

import pytest


EXPECTED_INDEXES = {
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

PLAN_CASES = [
    ("login-limiter", "SELECT COUNT(*) FROM login_failures WHERE ip=? AND failed_at>=?", ("ip", 0), "idx_login_failures_ip_time"),
    ("live-kanban", "SELECT t.* FROM kb_tasks t WHERE t.archived_at='' AND t.deleted_at='' ORDER BY t.id DESC", (), "idx_kb_tasks_live"),
    ("kanban-notifications", "SELECT t.id,t.title,t.due_date,t.archived_at,t.deleted_at,c.name FROM kb_tasks t LEFT JOIN kb_columns c ON c.id=t.column_id WHERE t.archived_at='' AND t.deleted_at='' AND t.due_date!='' AND t.due_date<=? ORDER BY t.due_date", ("2099-01-01",), "idx_kb_tasks_live_due"),
    ("kanban-trash", "SELECT t.*,(SELECT COUNT(*) FROM kb_task_members m WHERE m.task_id=t.id) n FROM kb_tasks t WHERE t.deleted_at!='' ORDER BY t.deleted_at DESC,t.id DESC", (), "idx_kb_tasks_deleted"),
    ("kanban-archive", "SELECT t.*,(SELECT COUNT(*) FROM kb_task_members m WHERE m.task_id=t.id) n FROM kb_tasks t WHERE t.archived_at!='' AND t.deleted_at='' ORDER BY t.archived_at DESC,t.id DESC", (), "idx_kb_tasks_archived"),
    ("todo-notifications", "SELECT id,title,due_date,'' AS col_name,'' AS emp FROM todo_items WHERE status!='done' AND due_date!='' AND due_date<=? ORDER BY due_date", ("2099-01-01",), "idx_todo_open_due"),
    ("todo-archive", "SELECT * FROM todo_items WHERE status='done' AND done_date=? ORDER BY id DESC", ("2026-01-01",), "idx_todo_done_date"),
    ("employee-meetings", "SELECT * FROM meetings WHERE employee_id=? ORDER BY date DESC,id DESC", (1,), "idx_meetings_employee_date"),
    ("employee-problems", "SELECT * FROM problems WHERE employee_id=? ORDER BY id DESC", (1,), "idx_problems_employee_id"),
    ("employee-history", "SELECT * FROM employee_history WHERE employee_id=? ORDER BY change_date DESC,id DESC", (1,), "idx_employee_history_employee_date"),
    ("year-record-count", "SELECT COUNT(*) FROM year_records WHERE year=?", (2026,), "idx_year_records_year"),
    ("kanban-column-order", "SELECT * FROM kb_columns WHERE kind='kanban' ORDER BY sort_order,id", (), "idx_kb_columns_kind_order"),
    ("todo-order", "SELECT * FROM todo_items WHERE status=? ORDER BY sort_order,id", ("backlog",), "idx_todo_status_order"),
]


@pytest.fixture
def db(app_env):
    connection = sqlite3.connect(app_env.DB_PATH)
    try:
        yield connection
    finally:
        connection.close()


def test_all_application_indexes_are_created(db):
    actual = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
    }
    assert EXPECTED_INDEXES <= actual, sorted(EXPECTED_INDEXES - actual)


@pytest.mark.parametrize(
    "_label,sql,params,index_name",
    PLAN_CASES,
    ids=[case[0] for case in PLAN_CASES],
)
def test_production_query_plan_uses_expected_index(db, _label, sql, params, index_name):
    detail = " | ".join(
        row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params)
    )
    assert index_name in detail, detail
