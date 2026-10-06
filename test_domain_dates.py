# -*- coding: utf-8 -*-
"""Remaining user-entered date fields reject malformed calendar dates."""
import os,sqlite3,tempfile
r=tempfile.mkdtemp(); os.environ['HR_DATA_DIR']=r; os.environ['HR_PASSWORD']='x'
import app
app.app.config['TESTING']=True; app.init_db()
d=sqlite3.connect(app.DB_PATH); d.execute("INSERT INTO employees(name,hire_date) VALUES('E','2020-01-01')"); eid=d.execute('select id from employees').fetchone()[0]; d.execute("INSERT INTO employee_history(employee_id,change_date) VALUES(?,'2020-01-01')",(eid,)); hid=d.execute('select id from employee_history').fetchone()[0]; d.execute("INSERT INTO meetings(employee_id,date) VALUES(?,'2020-01-01')",(eid,)); mid=d.execute('select id from meetings').fetchone()[0]; d.commit(); d.close()
c=app.app.test_client(); c.post('/login',data={'password':'x'})
cases=[
 ('hire add',c.post('/employee/new',data={'name':'Bad','hire_date':'2026-02-30'}),400),
 ('hire edit',c.post(f'/employee/{eid}/edit',data={'name':'E','hire_date':'2026-02-30'}),400),
 ('history add',c.post(f'/employee/{eid}/history/add',data={'change_date':'2026-02-30'}),400),
 ('history edit',c.post(f'/history/{hid}/edit',data={'change_date':'2026-02-30'}),400),
 ('meeting add',c.post(f'/employee/{eid}/meeting/new',data={'date':'2026-02-30'}),400),
 ('meeting edit',c.post(f'/meeting/{mid}/edit',data={'date':'2026-02-30'}),400),
]
for n,x,w in cases: print(('PASS' if x.status_code==w else 'FAIL'),n,x.status_code)
if any(x.status_code!=w for _,x,w in cases): raise SystemExit(1)
print('DOMAIN DATE VALIDATION OK')
