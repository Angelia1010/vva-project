"""职责的数据结构。

职责是“岗位上这个 AI 值守员要负责的事”，分三类：
  watch    盯守：发生了就通知（如有人吸烟）。后台观察每次看画面时一并判断。
  inspect  巡检：到点看一次、记录结果，异常才通知（如每天 9:00 读压力表）。
  report   汇报：到点生成报告发出去（如每天 18:00 的值守报告）。

长期职责由管理员配置；临时任务（temporary=True）由值班员用语音或钉钉/飞书下达，到期自动失效。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from vva.core.types import fmt_ts

KINDS = {"watch": "盯守", "inspect": "巡检", "report": "汇报"}
LEVELS = {"normal": "一般", "important": "重要", "urgent": "紧急"}
MODES = {"live": "正式", "trial": "试运行"}

# 通知对象的写法：
#   web               值守台（网页）弹出提醒
#   voice             现场 / 值守台语音播报
#   default           各接入入口（钉钉、飞书）配置的默认告警群
#   dingtalk:<chat>   指定的钉钉群（cid 开头）或员工 userId
#   feishu:<chat>     指定的飞书群（oc_ 开头）
DEFAULT_TARGETS = ["default", "web"]


def _hm(text: str) -> int | None:
    try:
        h, m = (int(x) for x in text.strip().split(":"))
    except ValueError:
        return None
    return h * 60 + m if 0 <= h <= 24 and 0 <= m < 60 else None


def parse_hours(text: str) -> list[tuple[int, int]]:
    """“08:00-18:00, 22:00-06:00” → [(480, 1080), (1320, 360)]（分钟数，可跨午夜）。空串表示全天。"""
    out = []
    for part in (text or "").replace("，", ",").split(","):
        if "-" not in part:
            continue
        a, b = (_hm(x) for x in part.split("-", 1))
        if a is not None and b is not None and a != b:
            out.append((a, b))
    return out


@dataclass
class Duty:
    id: str
    name: str
    condition: str                              # 看什么：能从画面直接判断的描述（巡检类为要检查的问题）
    kind: str = "watch"
    enabled: bool = True
    mode: str = "live"                          # live 正式 | trial 试运行（只记录不通知）
    temporary: bool = False
    expires: float = 0.0                        # 临时任务的到期时间
    hours: str = ""                             # 生效时间段，空为全天
    weekdays: list[int] = field(default_factory=list)   # 1~7，空为每天
    times: list[str] = field(default_factory=list)      # 巡检、汇报的执行时间，如 ["09:00", "15:00"]
    level: str = "normal"
    targets: list[str] = field(default_factory=list)    # 通知对象，空则用 DEFAULT_TARGETS
    voice_text: str = ""                        # 现场播报的话，空则播报提醒内容
    verify: bool = True                         # 命中后放大复核再通知
    min_duration_s: float = 0.0                 # 持续多久才算命中
    cooldown_s: float = 600.0                   # 同一件事多久内只报一次
    objects: list[str] = field(default_factory=list)    # 引用的技能目标（带上它们的参考图）
    rule_ref: str = ""                          # 关联的规章依据
    negatives: list[str] = field(default_factory=list)  # 误报截图（告警截图路径），复核时作为“不算”的例子
    source: str = "web"                         # web | voice | dingtalk | feishu | skill | template | legacy
    created_by: str = ""
    created: float = field(default_factory=time.time)
    last_triggered: float = 0.0
    trigger_count: int = 0
    last_run: float = 0.0                       # 巡检、汇报上次执行时间
    last_result: str = ""                       # 巡检上次结果

    # 兼容旧的监控规则接口（后台观察、技能按 condition / cooldown_s 使用）
    @property
    def kind_label(self) -> str:
        return KINDS.get(self.kind, self.kind)

    def effective_targets(self) -> list[str]:
        return list(self.targets) if self.targets else list(DEFAULT_TARGETS)

    def expired(self, now: float | None = None) -> bool:
        return self.temporary and self.expires > 0 and (now or time.time()) >= self.expires

    def in_schedule(self, now: float | None = None) -> bool:
        """当前是否在生效时间内（星期 + 时间段）。"""
        t = time.localtime(now or time.time())
        if self.weekdays and (t.tm_wday + 1) not in self.weekdays:
            return False
        spans = parse_hours(self.hours)
        if not spans:
            return True
        m = t.tm_hour * 60 + t.tm_min
        return any((a <= m < b) if a < b else (m >= a or m < b) for a, b in spans)

    def active(self, now: float | None = None) -> bool:
        """后台观察现在要不要判断这条职责（只对盯守类有意义）。"""
        return self.kind == "watch" and self.enabled and not self.expired(now) and self.in_schedule(now)

    def days_text(self) -> str:
        if not self.weekdays or len(self.weekdays) == 7:
            return "每天"
        if self.weekdays == [1, 2, 3, 4, 5]:
            return "工作日"
        if self.weekdays == [6, 7]:
            return "周末"
        return "每周" + "".join("一二三四五六日"[d - 1] for d in self.weekdays)

    def schedule_text(self) -> str:
        """生效时间，如“每天 全天”“工作日 08:00-18:00”“每天 09:00、15:00”。"""
        if self.kind in ("inspect", "report"):
            return f"{self.days_text()} {'、'.join(self.times)}" if self.times else "未设置时间"
        return f"{self.days_text()} {self.hours or '全天'}"

    def period_text(self, now: float | None = None) -> str:
        """职责期限：长期职责为生效时间；临时任务为“截止到 今天 18:00”。"""
        if not (self.temporary and self.expires):
            return self.schedule_text()
        now = now or time.time()
        t, n = time.localtime(self.expires), time.localtime(now)
        hm = time.strftime("%H:%M", t)
        days = (time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
                - time.mktime((n.tm_year, n.tm_mon, n.tm_mday, 0, 0, 0, 0, 0, -1))) / 86400
        if hm == "00:00":          # 午夜到期按“当天结束”说
            days, hm = days - 1, "24:00"
        day = {0: "今天", 1: "明天", 2: "后天"}.get(round(days), time.strftime("%m-%d", t))
        return f"截止到{day} {hm}"

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_record(cls, d: dict[str, Any]) -> Duty:
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        """给前端和智能体看的格式。"""
        d = self.to_record()
        d.update(kind_label=self.kind_label, level_label=LEVELS.get(self.level, self.level),
                 mode_label=MODES.get(self.mode, self.mode), schedule=self.schedule_text(),
                 expires_text=fmt_ts(self.expires) if self.expires else "",
                 last_triggered_text=fmt_ts(self.last_triggered) if self.last_triggered else "",
                 targets=self.effective_targets())
        return d
