from __future__ import annotations

import sqlite3
from pathlib import Path


class HistoryDB:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    preset TEXT NOT NULL,
                    input_bytes INTEGER NOT NULL,
                    output_bytes INTEGER NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    output_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'done',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def add_job(self, *, user_id: int, file_name: str, preset: str,
                input_bytes: int, output_bytes: int,
                input_sha256: str, output_sha256: str) -> None:
        with self.connect() as con:
            con.execute(
                """INSERT INTO jobs
                (user_id,file_name,preset,input_bytes,output_bytes,input_sha256,output_sha256,status)
                VALUES (?,?,?,?,?,?,?,'done')""",
                (user_id, file_name[:200], preset, input_bytes, output_bytes, input_sha256, output_sha256),
            )

    def recent(self, user_id: int, limit: int = 5) -> list[sqlite3.Row]:
        with self.connect() as con:
            return list(con.execute(
                "SELECT * FROM jobs WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
            ))
