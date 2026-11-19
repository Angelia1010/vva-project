"""录像的登记信息（data/ops.db）：切片、告警片段、清理记录、录像设置。

录像文件本身在磁盘上：
  <dir>/<摄像头>/<YYYYMMDD-HHMMSS>.mp4      普通录像，按分钟切片
  <dir>/_alerts/<告警编号>.mp4              告警前后的片段（转成 H.264，单独保留）
  <dir>/_cache/                            回放时临时生成的片段，1 小时后清理
"""
from __future__ import annotations

import json
import time
from typing import Any

from vva.core.sqlite import SqliteDB

SCHEMA = """
CREATE TABLE IF NOT EXISTS recording_segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, camera TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
  path TEXT NOT NULL, bytes INTEGER DEFAULT 0, codec TEXT DEFAULT '', low_fps INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_rec_seg ON recording_segments(camera, start);
CREATE INDEX IF NOT EXISTS idx_rec_seg_start ON recording_segments(start);
CREATE TABLE IF NOT EXISTS alert_clips (
  alert_id INTEGER PRIMARY KEY, camera TEXT, start REAL, end REAL, path TEXT, bytes INTEGER DEFAULT 0,
  keep_until REAL, created REAL);
CREATE TABLE IF NOT EXISTS recording_cleanups (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, reason TEXT, camera TEXT, detail TEXT, files INTEGER, freed_bytes INTEGER);
CREATE TABLE IF NOT EXISTS recording_settings (k TEXT PRIMARY KEY, v TEXT);
"""


class RecordingStore:
    def __init__(self, db: SqliteDB) -> None:
        self.db = db
        db.script(SCHEMA)

    # ---------------------------------------------------------------- 切片
    def add_segment(self, camera: str, start: float, end: float, path: str, size: int, codec: str = "",
                    low_fps: bool = False) -> int:
        return self.db.execute("INSERT INTO recording_segments(camera, start, end, path, bytes, codec, low_fps) "
                               "VALUES (?,?,?,?,?,?,?)", (camera, start, end, path, int(size), codec, int(low_fps)))

    def segments(self, camera: str, start: float, end: float) -> list[dict[str, Any]]:
        """和 [start, end] 有重叠的切片，按时间排序。"""
        return self.db.query("SELECT * FROM recording_segments WHERE camera = ? AND end > ? AND start < ? ORDER BY start",
                             (camera, start, end))

    def oldest(self, limit: int = 200, before: float | None = None, camera: str = "") -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM recording_segments WHERE 1=1", []
        if before is not None:
            sql += " AND start < ?"
            args.append(before)
        if camera:
            sql += " AND camera = ?"
            args.append(camera)
        return self.db.query(sql + " ORDER BY start LIMIT ?", [*args, limit])

    def delete_segments(self, ids: list[int]) -> None:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            self.db.execute(f"DELETE FROM recording_segments WHERE id IN ({','.join('?' * len(chunk))})", chunk)

    def usage(self) -> dict[str, dict[str, Any]]:
        """每路：总大小、切片数、最早、最新。"""
        rows = self.db.query("SELECT camera, SUM(bytes) AS bytes, COUNT(*) AS n, MIN(start) AS first, MAX(end) AS last "
                             "FROM recording_segments GROUP BY camera")
        return {r["camera"]: r for r in rows}

    def bytes_between(self, start: float, end: float) -> dict[str, int]:
        rows = self.db.query("SELECT camera, SUM(bytes) AS b FROM recording_segments WHERE start >= ? AND start < ? "
                             "GROUP BY camera", (start, end))
        return {r["camera"]: int(r["b"] or 0) for r in rows}

    def spans(self, camera: str, start: float, end: float, gap: float = 5.0) -> list[list[float]]:
        """把相邻的切片合并成连续时段 [[开始, 结束], ...]，用于画时间轴。"""
        out: list[list[float]] = []
        for r in self.db.query("SELECT start, end FROM recording_segments WHERE camera = ? AND end > ? AND start < ? "
                               "ORDER BY start", (camera, start, end)):
            s, e = max(r["start"], start), min(r["end"], end)
            if out and s - out[-1][1] <= gap:
                out[-1][1] = max(out[-1][1], e)
            else:
                out.append([s, e])
        return out

    def low_fps(self, camera: str) -> bool:
        r = self.db.query("SELECT low_fps FROM recording_segments WHERE camera = ? ORDER BY id DESC LIMIT 1", (camera,))
        return bool(r and r[0]["low_fps"])

    # ---------------------------------------------------------------- 告警片段
    def add_alert_clip(self, alert_id: int, camera: str, start: float, end: float, path: str, size: int,
                       keep_until: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO alert_clips(alert_id, camera, start, end, path, bytes, keep_until, created) "
                        "VALUES (?,?,?,?,?,?,?,?)", (alert_id, camera, start, end, path, int(size), keep_until, time.time()))

    def alert_clip(self, alert_id: int) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM alert_clips WHERE alert_id = ?", (int(alert_id),))
        return rows[0] if rows else None

    def expired_alert_clips(self, now: float) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM alert_clips WHERE keep_until < ?", (now,))

    def set_alert_clip_keep(self, alert_id: int, keep_until: float) -> None:
        self.db.execute("UPDATE alert_clips SET keep_until = ? WHERE alert_id = ?", (keep_until, int(alert_id)))

    def delete_alert_clip(self, alert_id: int) -> None:
        self.db.execute("DELETE FROM alert_clips WHERE alert_id = ?", (int(alert_id),))

    def alert_clip_stats(self) -> dict[str, int]:
        r = self.db.query("SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM alert_clips")[0]
        return {"count": int(r["n"]), "bytes": int(r["b"])}

    # ---------------------------------------------------------------- 清理记录
    def log_cleanup(self, reason: str, camera: str, detail: str, files: int, freed: int) -> None:
        self.db.execute("INSERT INTO recording_cleanups(ts, reason, camera, detail, files, freed_bytes) VALUES (?,?,?,?,?,?)",
                        (time.time(), reason, camera, detail, files, int(freed)))

    def cleanups(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM recording_cleanups ORDER BY id DESC LIMIT ?", (limit,))

    # ---------------------------------------------------------------- 设置
    def settings(self) -> dict[str, Any]:
        return {r["k"]: json.loads(r["v"]) for r in self.db.query("SELECT k, v FROM recording_settings")}

    def save_settings(self, values: dict[str, Any]) -> None:
        for k, v in values.items():
            self.db.execute("INSERT OR REPLACE INTO recording_settings(k, v) VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))
