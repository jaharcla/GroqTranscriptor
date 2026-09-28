import json
import sqlite3
import time
from pathlib import Path


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            path TEXT PRIMARY KEY, key TEXT, digest TEXT, page TEXT,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_retry REAL NOT NULL DEFAULT 0, error TEXT, pending TEXT
        )""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "routing" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN routing TEXT")
        self.db.commit()

    def get(self, path):
        row = self.db.execute("SELECT * FROM jobs WHERE path=?", (str(path),)).fetchone()
        return dict(row) if row else None

    def ensure(self, path):
        self.db.execute("INSERT OR IGNORE INTO jobs(path) VALUES (?)", (str(path),))
        self.db.commit()

    def set(self, path, **fields):
        allowed = {
            "key",
            "digest",
            "page",
            "status",
            "attempts",
            "next_retry",
            "error",
            "pending",
            "routing",
        }
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid state fields")
        sql = ", ".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE jobs SET {sql} WHERE path=?", (*fields.values(), str(path)))
        self.db.commit()

    def journal(self, path, operation):
        self.set(path, pending=json.dumps(operation) if operation else None)

    def fail(self, path, error, delay):
        row = self.get(path)
        attempts = row["attempts"] + 1
        self.set(
            path,
            status="failed",
            attempts=attempts,
            error=str(error),
            next_retry=time.time() + min(delay * 2 ** min(attempts - 1, 7), 3600),
        )

    def jobs(self, due=False):
        sql = "SELECT * FROM jobs"
        if due:
            sql += " WHERE status != 'done' AND next_retry <= ?"
        return [dict(row) for row in self.db.execute(sql, (time.time(),) if due else ())]

    def close(self):
        self.db.close()
