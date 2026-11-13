"""看图问答：统一的提问方式与结果解析，供实时画面工具和长期记忆工具共用。

看图模型按“依据 / 结论 / 把握”三行输出：先写证据再下结论，能明显减少数错、读错；
把握程度交给智能体，低把握时它会放大再看或如实说看不清。
"""
from __future__ import annotations

import re
from typing import Any

from vva.agent import prompts
from vva.core.jsonutil import strip_think

_FIELDS = {"依据": "evidence", "结论": "answer", "把握": "confidence"}
_LINE_RE = re.compile(r"^\s*[*#\-]*\s*(依据|结论|把握)[*\s]*[:：][*\s]*(.*)$")


def parse_look_answer(text: str) -> dict[str, str]:
    """解析三行格式；模型没按格式输出时，整段作为结论返回。"""
    text = strip_think(text)
    out: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m:
            current = _FIELDS[m.group(1)]
            out[current] = m.group(2).strip()
        elif current and line.strip():
            out[current] += line.strip()
    if not out.get("answer"):
        return {"answer": text.strip()}
    conf = out.get("confidence", "")
    out["confidence"] = next((c for c in ("高", "中", "低") if c in conf), conf)
    return out


async def ask_look(vlm: Any, images: list[bytes], question: str, scene_hint: str = "",
                   max_tokens: int | None = None) -> dict[str, str]:
    hint = f"场景说明：{scene_hint}\n" if scene_hint else ""
    text = await vlm.ask(images, prompts.LOOK_PROMPT.format(scene_hint=hint, question=question),
                         system=prompts.LOOK_SYSTEM, max_tokens=max_tokens)
    return parse_look_answer(text)
