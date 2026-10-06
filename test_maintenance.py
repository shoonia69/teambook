# -*- coding: utf-8 -*-
"""RED-регрессии: GET /board не изменяет БД, purge запускается обслуживанием."""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp()
os.environ["HR_DATA_DIR"] = _tmp
os.environ["HR_PASSWORD"] = "maintenance-test"
import app as appmod

appmod.app.config["TESTING"] = True
appmod.init_db()
failures = []

def check(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), "-", label, extra)
    if not cond: failures.append(label)

def count_task(title):
    c=sqlite3.connect(appmod.DB_PATH)
    try: return c.execute("SELECT COUNT(*) FROM kb_tasks WHERE title=?",(title,)).fetchone()[0]
    finally: c.close()

c=sqlite3.connect(appmod.DB_PATH)
c.execute("INSERT INTO kb_columns(name,kind,locked,sort_order) VALUES('Основная','kanban',0,0)")
cid=c.execute("SELECT id FROM kb_columns LIMIT 1").fetchone()[0]
now = datetime.utcnow()
old=(now-timedelta(days=30,seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
edge=(now-timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
new=(now-timedelta(days=29)).strftime("%Y-%m-%d %H:%M:%S")
c.execute("INSERT INTO kb_tasks(column_id,title,deleted_at) VALUES(?,?,?)",(cid,"OLD_TRASH",old))
c.execute("INSERT INTO kb_tasks(column_id,title,deleted_at) VALUES(?,?,?)",(cid,"EDGE_TRASH",edge))
c.execute("INSERT INTO kb_tasks(column_id,title,deleted_at) VALUES(?,?,?)",(cid,"NEW_TRASH",new))
c.execute("INSERT INTO kb_tasks(column_id,title,archived_at) VALUES(?,?,?)",(cid,"ARCHIVED",old))
c.execute("INSERT INTO kb_tasks(column_id,title) VALUES(?,?)",(cid,"ACTIVE"))
c.commit(); c.close()

client=appmod.app.test_client(); client.post('/login',data={'password':'maintenance-test'})
r=client.get('/board')
check("GET /board успешен",r.status_code==200,str(r.status_code))
check("GET /board не удаляет старую корзину",count_task("OLD_TRASH")==1)

appmod.run_maintenance(now_utc=now)
check("maintenance удаляет старую корзину",count_task("OLD_TRASH")==0)
check("граница 30 суток сохраняется",count_task("EDGE_TRASH")==1)
check("свежая корзина сохраняется",count_task("NEW_TRASH")==1)
check("архивная задача сохраняется",count_task("ARCHIVED")==1)
check("активная задача сохраняется",count_task("ACTIVE")==1)

print()
if failures:
    print("ИТОГ: ПРОВАЛЫ ->",failures); raise SystemExit(1)
print("ИТОГ: MAINTENANCE PACK OK")
