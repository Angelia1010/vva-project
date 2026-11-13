"""业务库（data/ops.db）的公共连接：单文件 SQLite，线程安全，所有调用都是毫秒级的小查询。"""
from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any


class SqliteDB:
    def __init__(self, path: str, schema: str = "") -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
        if schema:
            self.script(schema)

    def script(self, sql: str) -> None:
        with self._lock:
            self._db.executescript(sql)
            self._db.commit()

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        """执行一条写语句，返回最后插入的行号。"""
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return int(cur.lastrowid or 0)

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._db.close()
