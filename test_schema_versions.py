# -*- coding: utf-8 -*-
"""Schema migration ledger is created, bootstrapped and idempotent."""
import os,sqlite3,tempfile
root=tempfile.mkdtemp(); os.environ['HR_DATA_DIR']=root; os.environ['HR_PASSWORD']='x'
import app
app.init_db(); app.init_db()
c=sqlite3.connect(app.DB_PATH); c.row_factory=sqlite3.Row
rows=[tuple(r) for r in c.execute("SELECT version,name FROM schema_migrations ORDER BY version")]
assert rows==[(1,'baseline'),(2,'versioned-runner')],rows
before=c.total_changes; app._apply_migrations(c); c.commit()
rows2=[tuple(r) for r in c.execute("SELECT version,name FROM schema_migrations ORDER BY version")]
assert rows2==rows,rows2
# Legacy DB without ledger must be upgraded and then stamped.
legacy=tempfile.mkdtemp(); app.DATA_DIR=legacy; app.DB_PATH=os.path.join(legacy,'hr_notes.db')
d=sqlite3.connect(app.DB_PATH); d.row_factory=sqlite3.Row; d.executescript(app.SCHEMA); d.execute("DROP TABLE schema_migrations"); d.execute("INSERT INTO employees(name) VALUES('Legacy')"); d.commit(); d.close()
app.init_db(); d=sqlite3.connect(app.DB_PATH)
assert d.execute("SELECT name FROM employees").fetchone()[0]=='Legacy'
assert d.execute("SELECT version,name FROM schema_migrations ORDER BY version").fetchall()==[(1,'baseline'),(2,'versioned-runner')]
d.close(); print('MIGRATION LEDGER OK')

for rows in (((1,'attacker'),), ((2,'baseline'),), ((1,'baseline'),(3,'future'))):
    bad=tempfile.mkdtemp(); app.DATA_DIR=bad; app.DB_PATH=os.path.join(bad,'hr_notes.db')
    d=sqlite3.connect(app.DB_PATH); d.executescript(app.SCHEMA)
    d.execute("DELETE FROM schema_migrations")
    d.executemany("INSERT INTO schema_migrations(version,name) VALUES(?,?)",rows)
    d.commit(); d.close()
    try:
        app.init_db()
        raise AssertionError("forged ledger accepted: %r" % (rows,))
    except RuntimeError as exc:
        assert "schema_migrations" in str(exc) or "Неизвестная миграция" in str(exc)
print('FORGED LEDGERS REJECTED')
