# -*- coding: utf-8 -*-
"""Versioned migration runner applies each step exactly once and fails closed."""
import os,sqlite3,tempfile
root=tempfile.mkdtemp(); os.environ['HR_DATA_DIR']=root; os.environ['HR_PASSWORD']='x'
import app
app.init_db(); c=app._connect_db()
assert [tuple(r) for r in c.execute('select version,name from schema_migrations order by version')]==[(1,'baseline'),(2,'versioned-runner')]
c.close()
# Callback is run once; second run reads ledger and skips it.
calls=[]
def migration(db): calls.append(1); db.execute("CREATE TABLE runner_probe(id INTEGER)")
app._run_versioned_migrations(app._connect_db(), ((3,'probe',migration),))
d=app._connect_db(); app._run_versioned_migrations(d, ((3,'probe',migration),)); d.commit()
assert calls==[1],calls
assert [tuple(r) for r in d.execute("select version,name from schema_migrations order by version")][-1]==(3,'probe')
d.close()
# Failure must roll back both body and ledger row.
bad=app._connect_db()
def broken(db): db.execute("CREATE TABLE broken_probe(id INTEGER)"); raise RuntimeError('boom')
try: app._run_versioned_migrations(bad, ((4,'broken',broken),)); raise AssertionError('failure accepted')
except RuntimeError: pass
bad.rollback()
assert bad.execute("select 1 from sqlite_master where type='table' and name='broken_probe'").fetchone() is None
assert bad.execute("select 1 from schema_migrations where version=4").fetchone() is None
bad.close(); print('VERSIONED RUNNER OK')

# executescript implicitly commits and is forbidden inside versioned callbacks.
unsafe=app._connect_db()
unsafe.execute("DELETE FROM schema_migrations WHERE version=3")
def scripted(db):
    db.executescript("CREATE TABLE scripted_probe(id INTEGER); INSERT INTO scripted_probe VALUES(1);")
    raise RuntimeError('boom')
try:
    app._run_versioned_migrations(unsafe, ((4,'scripted',scripted),))
    raise AssertionError('executescript accepted')
except RuntimeError as exc:
    assert 'executescript' in str(exc)
assert unsafe.execute("select 1 from schema_migrations where version=4").fetchone() is None
unsafe.close(); print('UNSAFE SCRIPT REJECTED')

unsafe_ok=app._connect_db()
def scripted_ok(db):
    db.executescript("CREATE TABLE scripted_ok_probe(id INTEGER); INSERT INTO scripted_ok_probe VALUES(1);")
try:
    app._run_versioned_migrations(unsafe_ok, ((4,'scripted-ok',scripted_ok),))
    raise AssertionError('successful executescript accepted')
except RuntimeError as exc:
    assert 'executescript' in str(exc)
assert unsafe_ok.execute("select 1 from schema_migrations where version=4").fetchone() is None
unsafe_ok.close(); print('SUCCESSFUL UNSAFE SCRIPT REJECTED')
