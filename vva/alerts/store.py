"""告警的持久化：data/ops.db 的 alerts、alert_events 两张表；截图存在 data/alerts/<日期>/ 下。"""
from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any

from vva.alerts.model import Alert
from vva.core.sqlite import SqliteDB

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    duty_id TEXT NOT NULL,
    status  TEXT NOT NULL,
    data    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_duty ON alerts(duty_id, ts);
CREATE TABLE IF NOT EXISTS alert_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    ts       REAL NOT NULL,
    actor    TEXT NOT NULL,
    text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_events ON alert_events(alert_id);
"""


class AlertStore:
    def __init__(self, db: SqliteDB, image_dir: str) -> None:
        self.db = db
        self.image_dir = image_dir
        db.script(SCHEMA)

    # ---------------------------------------------------------------- 告警
    def insert(self, alert: Alert) -> Alert:
        data = alert.to_dict()
        alert.id = self.db.execute("INSERT INTO alerts(ts, duty_id, status, data) VALUES (?, ?, ?, ?)",
                                   (alert.ts, alert.duty_id, alert.status, json.dumps(data, ensure_ascii=False)))
        self.save(alert)
        return alert

    def save(self, alert: Alert) -> None:
        fields = {k: v for k, v in alert.to_dict().items() if k in Alert.__dataclass_fields__}
        self.db.execute("UPDATE alerts SET status = ?, data = ? WHERE id = ?",
                        (alert.status, json.dumps(fields, ensure_ascii=False), alert.id))

    @staticmethod
    def _row(r: dict[str, Any]) -> Alert:
        d = json.loads(r["data"])
        d["id"] = r["id"]
        return Alert(**{k: v for k, v in d.items() if k in Alert.__dataclass_fields__})

    def get(self, alert_id: int) -> Alert | None:
        rows = self.db.query("SELECT id, data FROM alerts WHERE id = ?", (alert_id,))
        return self._row(rows[0]) if rows else None

    def query(self, start: float = 0, end: float | None = None, duty_id: str = "", statuses: tuple[str, ...] = (),
              limit: int = 200) -> list[Alert]:
        sql, args = "SELECT id, data FROM alerts WHERE ts >= ? AND ts < ?", [start, end or time.time() + 1]
        if duty_id:
            sql += " AND duty_id = ?"
            args.append(duty_id)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [self._row(r) for r in self.db.query(sql, args)]

    def counts(self, start: float, end: float | None = None) -> dict[str, dict[str, int]]:
        """{duty_id: {status: 数量}}"""
        out: dict[str, dict[str, int]] = {}
        for r in self.db.query("SELECT duty_id, status, COUNT(*) AS n FROM alerts WHERE ts >= ? AND ts < ? "
                               "GROUP BY duty_id, status", (start, end or time.time() + 1)):
            out.setdefault(r["duty_id"], {})[r["status"]] = r["n"]
        return out

    # ---------------------------------------------------------------- 处理经过
    def add_event(self, alert_id: int, actor: str, text: str, ts: float | None = None) -> None:
        self.db.execute("INSERT INTO alert_events(alert_id, ts, actor, text) VALUES (?, ?, ?, ?)",
                        (alert_id, ts or time.time(), actor or "系统", text))

    def events(self, alert_id: int) -> list[dict[str, Any]]:
        return self.db.query("SELECT ts, actor, text FROM alert_events WHERE alert_id = ? ORDER BY id", (alert_id,))

    # ---------------------------------------------------------------- 截图
    def save_image(self, alert: Alert, jpeg: bytes, caption: str) -> str:
        day = time.strftime("%Y%m%d", time.localtime(alert.ts))
        rel = f"{day}/{alert.id}_{len(alert.images) + 1}.jpg"
        path = os.path.join(self.image_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(jpeg)
        alert.images.append({"file": rel, "caption": caption})
        return rel

    def image_path(self, rel: str) -> str | None:
        path = os.path.normpath(os.path.join(self.image_dir, rel))
        root = os.path.normpath(self.image_dir)
        return path if path.startswith(root + os.sep) and os.path.isfile(path) else None

    def cleanup(self, retention_days: float) -> int:
        """删除超过保留期的告警截图目录（记录本身保留，便于统计）。"""
        if retention_days <= 0 or not os.path.isdir(self.image_dir):
            return 0
        cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - retention_days * 86400))
        n = 0
        for name in os.listdir(self.image_dir):
            if name.isdigit() and len(name) == 8 and name < cutoff:
                shutil.rmtree(os.path.join(self.image_dir, name), ignore_errors=True)
                n += 1
        return n
