"""长期记忆的持久化存储（SQLite，单文件，无需额外服务）。

保存四类数据：
  events     画面记录（后台观察）、提醒触发、操作员指令，按时间索引
  summaries  按固定时段（如每 10 分钟）浓缩的摘要
  notes      操作员交代需要长期记住的信息
  rules      监控提醒规则，重启后恢复

只做数据读写，不依赖模型和事件总线。所有方法都是同步的小查询（毫秒级），可在事件循环中直接调用。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any

KIND_OBSERVATION = "observation"
KIND_ALERT = "alert"
KIND_COMMAND = "command"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,
    summary TEXT NOT NULL,
    people  INTEGER,
    details TEXT,
    frame   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS summaries (
    start   REAL PRIMARY KEY,
    end     REAL NOT NULL,
    text    TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    id   TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
"""


@dataclass
class MemoryEvent:
    ts: float
    kind: str
    summary: str
    people: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    frame: str | None = None          # 关键帧相对路径（相对于记忆目录）
    id: int | None = None


@dataclass
class Summary:
    start: float
    end: float
    text: str


@dataclass
class Note:
    id: int
    content: str
    created: float


class MemoryStore:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ---------------------------------------------------------------- events
    def add_event(self, ev: MemoryEvent) -> int:
        cur = self._exec(
            "INSERT INTO events(ts, kind, summary, people, details, frame) VALUES (?,?,?,?,?,?)",
            (ev.ts, ev.kind, ev.summary, ev.people, json.dumps(ev.details, ensure_ascii=False), ev.frame))
        return int(cur.lastrowid)

    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> MemoryEvent:
        return MemoryEvent(ts=r["ts"], kind=r["kind"], summary=r["summary"], people=r["people"],
                           details=json.loads(r["details"] or "{}"), frame=r["frame"], id=r["id"])

    def events_between(self, start: float, end: float, kinds: list[str] | None = None,
                       keyword: str | None = None) -> list[MemoryEvent]:
        sql, args = "SELECT * FROM events WHERE ts >= ? AND ts <= ?", [start, end]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        if keyword:
            sql += " AND (summary LIKE ? OR details LIKE ?)"
            args += [f"%{keyword}%", f"%{keyword}%"]
        return [self._row_to_event(r) for r in self._query(sql + " ORDER BY ts", tuple(args))]

    def nearest_frame(self, ts: float, max_gap: float) -> MemoryEvent | None:
        """离 ts 最近、且保存了关键帧的画面记录；相差超过 max_gap 秒则返回 None。"""
        rows = self._query(
            "SELECT * FROM events WHERE frame IS NOT NULL AND ts BETWEEN ? AND ? "
            "ORDER BY ABS(ts - ?) LIMIT 1", (ts - max_gap, ts + max_gap, ts))
        return self._row_to_event(rows[0]) if rows else None

    def time_range(self) -> tuple[float, float] | None:
        r = self._query("SELECT MIN(ts) AS a, MAX(ts) AS b FROM events")[0]
        return (r["a"], r["b"]) if r["a"] is not None else None

    def count_events(self) -> int:
        return int(self._query("SELECT COUNT(*) AS n FROM events")[0]["n"])

    # ---------------------------------------------------------------- summaries
    def add_summary(self, s: Summary) -> None:
        self._exec("INSERT OR REPLACE INTO summaries(start, end, text, created) VALUES (?,?,?,?)",
                   (s.start, s.end, s.text, time.time()))

    def has_summary(self, start: float) -> bool:
        return bool(self._query("SELECT 1 FROM summaries WHERE start = ?", (start,)))

    def summaries_between(self, start: float, end: float) -> list[Summary]:
        rows = self._query("SELECT * FROM summaries WHERE end > ? AND start < ? ORDER BY start", (start, end))
        return [Summary(r["start"], r["end"], r["text"]) for r in rows]

    # ---------------------------------------------------------------- notes
    def add_note(self, content: str) -> Note:
        now = time.time()
        cur = self._exec("INSERT INTO notes(content, created) VALUES (?, ?)", (content.strip(), now))
        return Note(int(cur.lastrowid), content.strip(), now)

    def delete_note(self, note_id: int) -> bool:
        return self._exec("DELETE FROM notes WHERE id = ?", (int(note_id),)).rowcount > 0

    def list_notes(self, limit: int = 50) -> list[Note]:
        rows = self._query("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (int(limit),))
        return [Note(r["id"], r["content"], r["created"]) for r in reversed(rows)]

    # ---------------------------------------------------------------- rules
    def save_rules(self, rules: list[dict[str, Any]]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM rules")
            self._db.executemany("INSERT INTO rules(id, data) VALUES (?, ?)",
                                 [(r["id"], json.dumps(r, ensure_ascii=False)) for r in rules])
            self._db.commit()

    def load_rules(self) -> list[dict[str, Any]]:
        return [json.loads(r["data"]) for r in self._query("SELECT data FROM rules ORDER BY rowid")]

    # ---------------------------------------------------------------- 清理
    def delete_events_before(self, ts: float) -> int:
        n = self._exec("DELETE FROM events WHERE ts < ?", (ts,)).rowcount
        self._exec("DELETE FROM summaries WHERE end < ?", (ts,))
        return n

    def frames_before(self, ts: float) -> list[str]:
        return [r["frame"] for r in self._query("SELECT frame FROM events WHERE frame IS NOT NULL AND ts < ?", (ts,))]

    def clear_frames_before(self, ts: float) -> None:
        self._exec("UPDATE events SET frame = NULL WHERE frame IS NOT NULL AND ts < ?", (ts,))
