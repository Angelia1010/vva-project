"""一句话 → 职责表单草稿。用大模型拆字段并改写判定内容，用户确认后才保存。"""
from __future__ import annotations

import logging
import re
from typing import Any

from vva.core.jsonutil import parse_json
from vva.duties import prompts
from vva.duties.model import KINDS, LEVELS, parse_hours
from vva.duties.templates import capability_hints
from vva.memory.summarizer import complete_text

log = logging.getLogger(__name__)

_TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _num(v: Any, default: float) -> float:
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return default


def _clean(data: dict[str, Any], text: str, target_ids: set[str]) -> dict[str, Any]:
    kind = data.get("kind") if data.get("kind") in KINDS else "watch"
    condition = str(data.get("condition") or text).strip()
    draft = {
        "name": str(data.get("name") or condition[:8]).strip()[:20],
        "kind": kind,
        "condition": condition,
        "hours": str(data.get("hours") or "") if parse_hours(str(data.get("hours") or "")) else "",
        "weekdays": sorted({int(d) for d in data.get("weekdays") or [] if str(d).isdigit() and 1 <= int(d) <= 7}),
        "times": [t for t in data.get("times") or [] if isinstance(t, str) and _TIME.match(t)],
        "level": data.get("level") if data.get("level") in LEVELS else "normal",
        "targets": [t for t in data.get("targets") or [] if t in target_ids],
        "voice_text": str(data.get("voice_text") or "")[:60],
        "min_duration_s": _num(data.get("min_duration_s"), 0.0),
        "cooldown_s": _num(data.get("cooldown_s"), 600.0) or 600.0,
        "objects": [str(o) for o in data.get("objects") or []][:3],
    }
    if kind != "watch" and not draft["times"]:
        draft["times"] = ["09:00"] if kind == "inspect" else ["18:00"]
    questions = [str(q) for q in data.get("questions") or [] if str(q).strip()][:3]
    if not draft["targets"] and kind != "inspect":
        questions.append("发现后通知谁？请在下方选择通知对象。")
    return {"draft": draft, "questions": list(dict.fromkeys(questions)),
            "hints": capability_hints(condition, kind)}


async def parse_duty(llm: Any, text: str, targets: list[dict[str, str]], objects: list[str], scene: str = "") -> dict[str, Any]:
    """targets: [{"id", "label"}]；返回 {"draft": {...}, "questions": [...], "hints": [...]}。"""
    text = text.strip()
    target_ids = {t["id"] for t in targets}
    prompt = prompts.PARSE_PROMPT.format(
        text=text, scene=f"场景：{scene}\n" if scene else "",
        targets="\n".join(f"- {t['id']}：{t['label']}" for t in targets) or "（没有可选的通知对象）",
        objects="、".join(objects) or "无")
    try:
        raw = await complete_text(llm, [{"role": "system", "content": prompts.PARSE_SYSTEM},
                                        {"role": "user", "content": prompt}])
        data = parse_json(raw)
    except Exception as e:
        log.warning("解析职责描述失败：%s", e)
        data = None
    if not isinstance(data, dict):
        data = {"condition": text}
    return _clean(data, text, target_ids)
