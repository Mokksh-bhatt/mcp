import sqlite3
from contextlib import closing

DB_PATH = 'data/reminders.db'

def query(sql, params=(), fetch_one=False, commit=False):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        if commit:
            conn.commit()
            return None
        rows = cur.fetchall()
        return rows[0] if fetch_one and rows else rows