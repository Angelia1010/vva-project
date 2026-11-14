"""通知路由。"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from vva.core.bus import TOPIC_ALERT, EventBus

log = logging.getLogger(__name__)

CHANNEL_LABELS = {"dingtalk": "钉钉", "feishu": "飞书"}


def describe_target(target: str, labels: dict[str, str] | None = None) -> str:
    """把通知对象写成人能看懂的样子。labels：{target: 名称}（如群名）。"""
    if labels and target in labels:
        return labels[target]
    if target == "web":
        return "值守台"
    if target == "voice":
        return "现场播报"
    if target == "default":
        # labels 由应用提供：设置了默认告警群时会有 "default" 这一项；没有就说明实际没发出去
        return "默认告警群" if labels is None or "default" in labels else "默认告警群（未设置，没有发出）"
    ch, _, chat = target.partition(":")
    return f"{CHANNEL_LABELS.get(ch, ch)}·{chat[:10]}" if chat else CHANNEL_LABELS.get(ch, target)


class NotifyRouter:
    def __init__(self, bus: EventBus, station_name: Callable[[], str] = lambda: "",
                 labels: Callable[[], dict[str, str]] | None = None) -> None:
        self.bus = bus
        self.station_name = station_name
        self.labels = labels

    def _labels(self) -> dict[str, str]:
        return self.labels() if self.labels else {}

    def dispatch_alert(self, alert: Any, duty: Any, events: Callable[[str], None] | None = None) -> None:
        """告警：岗位、职责、依据、截图由各入口按自己的格式发送。"""
        targets = duty.effective_targets()
        payload = {
            "kind": "alert", "alert_id": alert.id, "rule_id": duty.id, "duty_id": duty.id, "name": duty.name,
            "condition": duty.name if duty.name else duty.condition, "detail": duty.condition,
            "evidence": alert.evidence + (f"（依据：{duty.rule_ref}）" if duty.rule_ref else ""),
            "level": duty.level, "station": self.station_name(), "ts": alert.ts, "targets": targets,
            "voice_text": duty.voice_text, "image": alert.images[-1]["file"] if alert.images else "",
        }
        self.bus.publish(TOPIC_ALERT, payload)
        if events:
            labels = self._labels()
            events("已通知 " + "、".join(describe_target(t, labels) for t in targets))
        log.info("告警 #%s %s → %s", alert.id, duty.name, targets)

    def dispatch_report(self, duty: Any, title: str, text: str) -> None:
        self.bus.publish(TOPIC_ALERT, {"kind": "report", "duty_id": duty.id, "name": duty.name, "title": title,
                                       "text": text, "targets": duty.effective_targets(), "ts": time.time(),
                                       "station": self.station_name()})
