"""检索索引：后台观察写下的每条画面记录，按摄像头、时间、文字检索（data/ops.db）。

中文按“相邻两个字”切词后放进 SQLite FTS5 全文索引：搜“吸烟”“叉车经过”都能命中，不依赖分词库。
SQLite 没有 FTS5 时退回 LIKE 查询（慢一些，结果一样）。

告警不在这里建索引：告警本来就存在 alerts 表里，检索时直接查。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from vva.core.sqlite import SqliteDB

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS search_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, camera TEXT NOT NULL DEFAULT '', camera_name TEXT DEFAULT '',
  ts REAL NOT NULL, kind TEXT NOT NULL DEFAULT 'observation', text TEXT NOT NULL, people INTEGER, grams TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_search_ts ON search_entries(ts);
CREATE INDEX IF NOT EXISTS idx_search_cam ON search_entries(camera, ts);
"""
FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(grams, content='search_entries', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS search_ai AFTER INSERT ON search_entries BEGIN
  INSERT INTO search_fts(rowid, grams) VALUES (new.id, new.grams); END;
CREATE TRIGGER IF NOT EXISTS search_ad AFTER DELETE ON search_entries BEGIN
  INSERT INTO search_fts(search_fts, rowid, grams) VALUES ('delete', old.id, old.grams); END;
"""

_CJK = re.compile(r"[㐀-鿿]")
_WORD = re.compile(r"[㐀-鿿]+|[A-Za-z0-9]+")


def grams(text: str) -> str:
    """“有人吸烟 ok” → “有人 人吸 吸烟 ok”：中文切成相邻两字，英文数字按词，全部小写。"""
    out: list[str] = []
    for w in _WORD.findall(text or ""):
        if _CJK.match(w):
            out += [w[i:i + 2] for i in range(len(w) - 1)] if len(w) > 1 else [w]
        else:
            out.append(w.lower())
    return " ".join(out)


def _fts_query(keyword: str) -> str | None:
    g = grams(keyword).split()
    if not g or any(len(x) < 2 and _CJK.match(x) for x in g):      # 单个汉字没法用二字索引查
        return None
    return " AND ".join(f'"{x}"' for x in g)


@dataclass
class Entry:
    id: int
    camera: str
    camera_name: str
    ts: float
    kind: str
    text: str
    people: int | None = None
    matched: list[str] = field(default_factory=list)


@dataclass
class Query:
    start: float = 0.0
    end: float = 0.0
    cameras: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)      # 任意一个命中即可（同义词）
    people_min: int | None = None
    limit: int = 400


class SearchIndex:
    def __init__(self, db: SqliteDB) -> None:
        self.db = db
        db.script(SCHEMA)
        try:
            db.script(FTS)
            self.fts = True
        except sqlite3.OperationalError as e:
            log.warning("SQLite 不支持 FTS5，检索改用 LIKE：%s", e)
            self.fts = False

    def add(self, camera: str, ts: float, text: str, people: int | None = None, kind: str = "observation",
            camera_name: str = "") -> int:
        return self.db.execute("INSERT INTO search_entries(camera, camera_name, ts, kind, text, people, grams) "
                               "VALUES (?,?,?,?,?,?,?)", (camera or "", camera_name, ts, kind, text, people, grams(text)))

    def count(self) -> int:
        return int(self.db.query("SELECT COUNT(*) AS n FROM search_entries")[0]["n"])

    def latest_ts(self) -> float:
        r = self.db.query("SELECT MAX(ts) AS t FROM search_entries")[0]["t"]
        return float(r or 0)

    def delete_before(self, ts: float) -> int:
        n = int(self.db.query("SELECT COUNT(*) AS n FROM search_entries WHERE ts < ?", (ts,))[0]["n"])
        if n:
            self.db.execute("DELETE FROM search_entries WHERE ts < ?", (ts,))
        return n

    def query(self, q: Query) -> list[Entry]:
        """按时间先后返回命中的记录；没有关键词时只按条件筛（按时间均匀取 limit 条）。"""
        end = q.end or time.time() + 1
        where, args = ["e.ts >= ?", "e.ts <= ?"], [q.start, end]
        if q.cameras:
            where.append(f"e.camera IN ({','.join('?' * len(q.cameras))})")
            args += list(q.cameras)
        if q.people_min is not None:
            where.append("e.people >= ?")
            args.append(int(q.people_min))
        base = " AND ".join(where)
        if not q.keywords:
            rows = self.db.query(f"SELECT e.* FROM search_entries e WHERE {base} ORDER BY e.ts", args)
            if len(rows) > q.limit:
                step = len(rows) / q.limit
                rows = [rows[int(i * step)] for i in range(q.limit)]
            return [self._entry(r, []) for r in rows]
        found: dict[int, Entry] = {}
        for kw in dict.fromkeys(k.strip() for k in q.keywords if k and k.strip()):
            fq = _fts_query(kw) if self.fts else None
            if fq:
                sql = (f"SELECT e.* FROM search_entries e JOIN search_fts f ON f.rowid = e.id "
                       f"WHERE search_fts MATCH ? AND {base} ORDER BY e.ts LIMIT ?")
                rows = self.db.query(sql, [fq, *args, q.limit])
            else:
                rows = self.db.query(f"SELECT e.* FROM search_entries e WHERE e.text LIKE ? AND {base} ORDER BY e.ts LIMIT ?",
                                     [f"%{kw}%", *args, q.limit])
            for r in rows:
                e = found.get(r["id"]) or found.setdefault(r["id"], self._entry(r, []))
                e.matched.append(kw)
        return sorted(found.values(), key=lambda e: e.ts)[: q.limit]

    @staticmethod
    def _entry(r: dict[str, Any], matched: list[str]) -> Entry:
        return Entry(id=r["id"], camera=r["camera"], camera_name=r["camera_name"] or "", ts=r["ts"], kind=r["kind"],
                     text=r["text"], people=r["people"], matched=matched)


def entry_text(summary: str, objects: list[str] | None = None, activities: list[str] | None = None,
               anomalies: list[str] | None = None) -> str:
    parts = [summary.strip()]
    if activities:
        parts.append("动作：" + "、".join(activities))
    if anomalies:
        parts.append("异常：" + "、".join(anomalies))
    if objects:
        parts.append("物体：" + "、".join(objects))
    return "；".join(p for p in parts if p)
