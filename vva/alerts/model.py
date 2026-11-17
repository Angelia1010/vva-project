"""告警记录。

状态：
  pending   待处理（已通知）
  handling  处理中（有人点了“收到 / 处理中”）
  resolved  已处理
  false     误报
  trial     试运行命中（只记录不通知，等人标注准不准）
  record    巡检的正常记录（不算告警，只用于展示巡检结果）
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from vva.core.types import fmt_ts

STATUS = {"pending": "待处理", "handling": "处理中", "resolved": "已处理", "false": "误报",
          "trial": "试运行", "record": "记录"}
OPEN = ("pending", "handling")

# 处理动作 → 新状态
ACTIONS = {"ack": "handling", "handling": "handling", "resolve": "resolved", "false": "false",
           "trial_ok": "resolved"}
ACTION_TEXT = {"ack": "收到", "handling": "处理中", "resolve": "已处理", "false": "标为误报", "trial_ok": "标为准确"}


@dataclass
class Alert:
    id: int
    ts: float
    duty_id: str
    duty_name: str
    condition: str
    kind: str = "watch"                 # watch 盯守告警 | inspect 巡检
    level: str = "normal"
    status: str = "pending"
    evidence: str = ""
    box: list[float] | None = None      # 目标位置（0~1，x1 y1 x2 y2）
    images: list[dict[str, str]] = field(default_factory=list)   # [{"file", "caption"}]
    targets: list[str] = field(default_factory=list)
    rule_ref: str = ""
    handler: str = ""
    handled_ts: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(time=fmt_ts(self.ts), status_label=STATUS.get(self.status, self.status),
                 handled_text=fmt_ts(self.handled_ts) if self.handled_ts else "")
        return d
