"""岗位：一个摄像头点位的基本信息。值守台首屏、通知标题、智能体和后台观察的场景说明都用它。

保存在 data/ops.db 的 station 表；首次启动用 config.yaml 的 station 段作为初始值。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any

from vva.core.sqlite import SqliteDB

SCHEMA = "CREATE TABLE IF NOT EXISTS station (id TEXT PRIMARY KEY, data TEXT NOT NULL);"


@dataclass
class Station:
    name: str = "1 号点位"
    area: str = ""          # 看哪里：画面覆盖的区域
    scene: str = ""         # 场景说明：规章、常识，所有职责共用
    owner: str = ""         # 岗位负责人
    paused: bool = False    # 暂停值守（所有职责都不判断）

    def prompt_block(self) -> str:
        parts = [f"岗位：{self.name}"]
        if self.area:
            parts.append(f"画面范围：{self.area}")
        if self.scene:
            parts.append(f"场景说明：{self.scene}")
        return "\n".join(parts)


class StationStore:
    def __init__(self, db: SqliteDB, defaults: dict[str, Any] | None = None, station_id: str = "default") -> None:
        self.db, self.id = db, station_id
        db.script(SCHEMA)
        rows = db.query("SELECT data FROM station WHERE id = ?", (station_id,))
        known = {f.name for f in fields(Station)}
        data = json.loads(rows[0]["data"]) if rows else {k: v for k, v in (defaults or {}).items() if k in known}
        self.station = Station(**{k: v for k, v in data.items() if k in known})
        if not rows:
            self.save()

    def save(self) -> None:
        self.db.execute("INSERT OR REPLACE INTO station(id, data) VALUES (?, ?)",
                        (self.id, json.dumps(asdict(self.station), ensure_ascii=False)))

    def update(self, **changes: Any) -> Station:
        for k, v in changes.items():
            if k in Station.__dataclass_fields__:
                setattr(self.station, k, type(getattr(self.station, k))(v))
        self.save()
        return self.station

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.station)


__all__ = ["Station", "StationStore"]
