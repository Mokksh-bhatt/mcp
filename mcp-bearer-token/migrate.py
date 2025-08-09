import sqlite3
import os

db_dir = 'data'
if not os.path.exists(db_dir):
    os.makedirs(db_dir)

conn = sqlite3.connect(os.path.join(db_dir, 'reminders.db'))
cur = conn.cursor()
cur.execute('''
CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    text TEXT NOT NULL,
    due_at INTEGER NOT NULL,
    timezone TEXT,
    created_at INTEGER NOT NULL,
    sent INTEGER DEFAULT 0,
    calendar_event_id TEXT
)
''')

cur.execute('''
CREATE TABLE IF NOT EXISTS oauth_tokens (
    phone TEXT PRIMARY KEY,
    credentials TEXT
)
''')

conn.commit()
conn.close()
print('Migration complete')