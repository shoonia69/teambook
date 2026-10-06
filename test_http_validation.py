# -*- coding: utf-8 -*-
"""HTTP validation must reject bad dates/ranges without leaking SQLite 500s."""
import os, sqlite3, tempfile

root=tempfile.mkdtemp(); os.environ['HR_DATA_DIR']=root; os.environ['HR_PASSWORD']='x'
import app
app.app.config['TESTING']=True; app.init_db()
db=sqlite3.connect(app.DB_PATH)
db.execute("INSERT INTO employees(name) VALUES('Тест')")
eid=db.execute("SELECT id FROM employees").fetchone()[0]
db.execute("INSERT INTO kb_columns(name) VALUES('Работа')")
cid=db.execute("SELECT id FROM kb_columns").fetchone()[0]
db.execute("INSERT INTO todo_items(title) VALUES('Todo')")
tid=db.execute("SELECT id FROM todo_items").fetchone()[0]
db.execute("INSERT INTO kb_tasks(column_id,title,start_date,due_date) VALUES(?,?,?,?)",
           (cid,'Existing','2026-01-01','2026-01-02'))
ktid=db.execute("SELECT id FROM kb_tasks WHERE title='Existing'").fetchone()[0]
db.commit(); db.close()
c=app.app.test_client(); c.post('/login',data={'password':'x'})
checks=[]
def ok(label,response,expected=400):
    checks.append((label,response.status_code==expected,response.status_code))

ok('semester',c.post(f'/employee/{eid}/record',data={'year':'2026','semester':'3H'}))
ok('board invalid date',c.post('/board/task/add',data={'title':'X','column_id':str(cid),'start_date':'2026-02-30','due_date':''}))
ok('board reversed',c.post('/board/task/add',data={'title':'X','column_id':str(cid),'start_date':'2026-03-02','due_date':'2026-03-01'}))
ok('board invalid edit',c.post(f'/board/task/{ktid}/edit',data={'title':'Changed','column_id':str(cid),'start_date':'2026-03-02','due_date':'2026-03-01'}))
ok('todo invalid date',c.post(f'/todo/{tid}/edit',data={'title':'Todo','due_date':'2026-02-30','tag':''}))
db=sqlite3.connect(app.DB_PATH)
row=db.execute("SELECT title,start_date,due_date FROM kb_tasks WHERE id=?",(ktid,)).fetchone()
checks.append(('invalid edit preserves row',row==('Existing','2026-01-01','2026-01-02'),row))
created=db.execute("SELECT COUNT(*) FROM kb_tasks WHERE title='X'").fetchone()[0]
checks.append(('invalid add creates no row',created==0,created))
db.close()
for label,passed,status in checks: print(('PASS' if passed else 'FAIL'),label,status)
if not all(x[1] for x in checks): raise SystemExit(1)
print('HTTP VALIDATION OK')
