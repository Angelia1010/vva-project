"""行业模板：常用职责的预设。判定内容已改写成能从画面直接判断的描述，参数按经验给了默认值。

每项可直接作为 DutyBook.create() 的参数；hint 是给管理员看的能力提示。
"""
from __future__ import annotations

from typing import Any

TEMPLATES: dict[str, dict[str, Any]] = {
    "warehouse": {"name": "仓库", "items": [
        {"name": "禁止吸烟", "condition": "画面中有人吸烟，或手里夹着点燃的香烟（可见烟头火光或烟雾）", "level": "urgent",
         "voice_text": "仓库内禁止吸烟", "cooldown_s": 600, "hint": "香烟属于小目标，人离镜头超过 10 米时识别率下降，建议先试运行"},
        {"name": "明火烟雾", "condition": "画面中出现明火，或有明显的烟雾升起", "level": "urgent", "cooldown_s": 300},
        {"name": "通道堆物", "condition": "画面中的通道（地面标线之间的区域）被货物、托盘或杂物占用", "level": "normal",
         "min_duration_s": 600, "cooldown_s": 3600, "hint": "需要在场景说明里写清通道在画面中的位置"},
        {"name": "叉车通道行人", "condition": "叉车通道内有行人停留或行走", "level": "important", "cooldown_s": 300},
        {"name": "人员倒地", "condition": "画面中有人倒在地上或躺在地上不动", "level": "urgent", "min_duration_s": 10,
         "cooldown_s": 300},
        {"name": "夜间闯入", "condition": "画面中出现人员", "hours": "22:00-06:00", "level": "urgent", "cooldown_s": 600,
         "voice_text": "您已进入监控区域，请立即离开"},
        {"name": "下班门未关", "kind": "inspect", "condition": "仓库大门是否关闭？大门敞开即为异常", "times": ["18:30"],
         "level": "important"},
        {"name": "每日值守报告", "kind": "report", "condition": "汇总当天的告警和画面情况", "times": ["18:00"]},
    ]},
    "factory": {"name": "工厂车间", "items": [
        {"name": "未戴安全帽", "condition": "画面中有人头上没有戴安全帽", "level": "important", "cooldown_s": 600,
         "voice_text": "进入车间请佩戴安全帽", "hint": "人员背对或距离较远时容易误判，建议先试运行"},
        {"name": "禁止吸烟", "condition": "画面中有人吸烟，或手里夹着点燃的香烟（可见烟头火光或烟雾）", "level": "urgent",
         "cooldown_s": 600},
        {"name": "明火烟雾", "condition": "画面中出现明火，或有明显的烟雾升起", "level": "urgent", "cooldown_s": 300},
        {"name": "人员倒地", "condition": "画面中有人倒在地上或躺在地上不动", "level": "urgent", "min_duration_s": 10,
         "cooldown_s": 300},
        {"name": "离岗提醒", "condition": "工位上没有人", "hours": "08:00-18:00", "weekdays": [1, 2, 3, 4, 5],
         "min_duration_s": 600, "cooldown_s": 1800, "hint": "需要在场景说明里写清工位在画面中的位置"},
        {"name": "仪表巡检", "kind": "inspect", "condition": "读取压力表读数，超过 0.6MPa 为异常", "times": ["09:00", "15:00"],
         "level": "important"},
        {"name": "每日值守报告", "kind": "report", "condition": "汇总当天的告警和画面情况", "times": ["18:00"]},
    ]},
    "office": {"name": "办公室", "items": [
        {"name": "陌生人进入", "condition": "画面中出现未佩戴工牌的人员", "level": "important", "cooldown_s": 600},
        {"name": "下班后有人", "condition": "画面中出现人员", "hours": "21:00-07:00", "level": "important", "cooldown_s": 1800},
        {"name": "每日值守报告", "kind": "report", "condition": "汇总当天的告警和画面情况", "times": ["18:00"]},
    ]},
}

# 判定内容里出现这些词时，提示管理员先试运行
SMALL_TARGETS = ("烟", "手机", "工牌", "安全帽", "口罩", "手套", "读数", "文字", "数字", "刀")


def capability_hints(condition: str, kind: str = "watch") -> list[str]:
    hints = []
    if kind == "watch" and any(w in condition for w in SMALL_TARGETS):
        hints.append("要识别的是小目标或细节，人离镜头较远、遮挡、光线差时识别率会下降，建议先试运行一两天，或上传几张参考图。")
    if any(w in condition for w in ("谁", "身份", "是不是张", "认识")):
        hints.append("不支持识别具体是谁（人脸身份），可以改成按衣着、工牌、位置来描述。")
    if any(w in condition for w in ("一闪", "瞬间", "跑过")):
        hints.append("后台每 5~10 秒看一次画面，一闪而过的动作可能漏掉。")
    return hints


def template_list() -> list[dict[str, Any]]:
    return [{"id": k, "name": v["name"], "items": v["items"]} for k, v in TEMPLATES.items()]
