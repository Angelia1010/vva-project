"""岗位：一个现场（如“1 号仓库”）。一个岗位下可以有多个摄像头（多个角度），见 vva.cameras。

岗位记现场共用的信息：名称、负责人、场景说明（规章、常识），以及“暂停值守”。
每个摄像头自己记“看哪里”。旧版只有一个岗位（id=default），升级后原样保留。

保存在 data/ops.db 的 station 表；首次启动用 config.yaml 的 station 段作为默认岗位的初始值。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable

from vva.core.sqlite import SqliteDB

SCHEMA = "CREATE TABLE IF NOT EXISTS station (id TEXT PRIMARY KEY, data TEXT NOT NULL);"
DEFAULT_ID = "default"


@dataclass
class Station:
    name: str = "1 号点位"
    area: str = ""          # 看哪里（旧版的单摄像头用；多摄像头时各摄像头自己记）
    scene: str = ""         # 场景说明：规章、常识，这个岗位的所有职责共用
    owner: str = ""         # 岗位负责人
    paused: bool = False    # 暂停值守（这个岗位的摄像头都不判断职责）
    id: str = DEFAULT_ID
    sort: int = 0
    created: float = field(default_factory=time.time)

    def prompt_block(self) -> str:
        parts = [f"岗位：{self.name}"]
        if self.area:
            parts.append(f"画面范围：{self.area}")
        if self.scene:
            parts.append(f"场景说明：{self.scene}")
        return "\n".join(parts)


EDITABLE = ("name", "area", "scene", "owner", "paused", "sort")


class StationStore:
    """岗位清单。`station` 属性是默认岗位（兼容旧的单岗位接口）。"""

    def __init__(self, db: SqliteDB, defaults: dict[str, Any] | None = None, station_id: str = DEFAULT_ID) -> None:
        self.db, self.id = db, station_id
        db.script(SCHEMA)
        self._known = {f.name for f in fields(Station)}
        self._items: dict[str, Station] = {}
        for r in db.query("SELECT id, data FROM station ORDER BY rowid"):
            data = {k: v for k, v in json.loads(r["data"]).items() if k in self._known}
            data["id"] = r["id"]
            self._items[r["id"]] = Station(**data)
        if station_id not in self._items:
            data = {k: v for k, v in (defaults or {}).items() if k in self._known and k != "id"}
            self._items[station_id] = Station(**data, id=station_id)
            self._save(self._items[station_id])
        self._listeners: list[Callable[[], None]] = []

    # ---------------------------------------------------------------- 读
    @property
    def station(self) -> Station:
        return self._items.get(self.id) or next(iter(self._items.values()))

    def list(self) -> list[Station]:
        return sorted(self._items.values(), key=lambda s: (s.sort, s.created))

    def get(self, station_id: str | None) -> Station | None:
        return self._items.get(station_id or "")

    def to_dict(self, station_id: str | None = None) -> dict[str, Any]:
        st = self.get(station_id) or self.station
        return asdict(st)

    # ---------------------------------------------------------------- 写
    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in self._listeners:
            fn()

    def _save(self, st: Station) -> None:
        self.db.execute("INSERT OR REPLACE INTO station(id, data) VALUES (?, ?)",
                        (st.id, json.dumps(asdict(st), ensure_ascii=False)))

    def save(self) -> None:
        self._save(self.station)

    def _apply(self, st: Station, changes: dict[str, Any]) -> None:
        for k, v in changes.items():
            if k in EDITABLE:
                setattr(st, k, type(getattr(st, k))(v))

    def update(self, station_id: str | None = None, **changes: Any) -> Station:
        """修改岗位。不传编号时改默认岗位（兼容旧接口 update(name=...)）。"""
        st = self.get(station_id) if station_id else self.station
        if st is None:
            raise KeyError(station_id)
        self._apply(st, changes)
        self._save(st)
        self._changed()
        return st

    def create(self, **fields_: Any) -> Station:
        name = str(fields_.get("name") or "").strip()
        if not name:
            raise ValueError("请填写岗位名称")
        sid = self._new_id(name)
        st = Station(name=name, id=sid, sort=len(self._items))
        self._apply(st, {k: v for k, v in fields_.items() if k != "name"})
        self._items[sid] = st
        self._save(st)
        self._changed()
        return st

    def remove(self, station_id: str) -> bool:
        if station_id == self.id or station_id not in self._items:
            return False
        del self._items[station_id]
        self.db.execute("DELETE FROM station WHERE id = ?", (station_id,))
        self._changed()
        return True

    def _new_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "", name.lower())[:12] or "s"
        n = len(self._items) + 1
        while f"{base}{n}" in self._items:
            n += 1
        return f"{base}{n}"

    def all_paused(self) -> bool:
        return all(s.paused for s in self._items.values())


__all__ = ["DEFAULT_ID", "Station", "StationStore"]
