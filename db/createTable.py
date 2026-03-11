import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DB_FILE = BASE_DIR / "db" / "database.db"

DB_FILE.parent.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(DB_FILE)
cursor = conn.cursor()

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS user_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type INTEGER NOT NULL,
    filePath TEXT NOT NULL,
    userName TEXT NOT NULL,
    status INTEGER DEFAULT 0
)
"""
)

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS file_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    filesize REAL,
    upload_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    file_path TEXT
)
"""
)

cursor.execute("PRAGMA table_info(file_records)")
columns = {row[1] for row in cursor.fetchall()}
if "source_url" not in columns:
    cursor.execute("ALTER TABLE file_records ADD COLUMN source_url TEXT")

cursor.execute(
    """
CREATE INDEX IF NOT EXISTS idx_file_records_source_url
ON file_records(source_url)
"""
)

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS publish_task_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_id INTEGER,
    account_id INTEGER NOT NULL,
    account_name TEXT NOT NULL,
    platform_name TEXT NOT NULL,
    platform_type INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT '待发布',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    error_msg TEXT
)
"""
)

conn.commit()
print("✅ 表创建成功")
conn.close()
