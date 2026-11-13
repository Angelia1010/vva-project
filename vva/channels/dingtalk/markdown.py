"""钉钉消息内容。机器人消息用 sampleMarkdown（标题 + Markdown 正文），画面单独用 sampleImageMsg 发送。"""
from __future__ import annotations

from typing import Any


def _link(web_url: str) -> str:
    return f"\n\n[查看现场]({web_url})" if web_url else ""


def answer(text: str, caption: str = "") -> dict[str, Any]:
    body = text or "（没有回答）"
    if caption:
        body += f"\n\n> {caption}"
    return {"title": (text or "监控助手")[:30], "text": body}


def alert(condition: str, evidence: str, time_text: str, web_url: str = "") -> dict[str, Any]:
    lines = ["### 监控提醒", f"**触发条件**：{condition}", f"**时间**：{time_text}"]
    if evidence:
        lines.append(f"**依据**：{evidence}")
    lines.append("> 在群里 @我 可以追问，如“现在还有人吗”")
    return {"title": f"监控提醒：{condition}"[:30], "text": "\n\n".join(lines) + _link(web_url)}


def report(title: str, text: str, web_url: str = "") -> dict[str, Any]:
    return {"title": title[:30], "text": f"### {title}\n\n{text or '这段时间没有记录。'}{_link(web_url)}"}
