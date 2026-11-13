"""飞书消息卡片（只用文字、图片和链接按钮，不需要配置卡片回调）。"""
from __future__ import annotations

from typing import Any


def _card(title: str, template: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {"config": {"wide_screen_mode": True},
            "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def _md(text: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def _img(image_key: str, alt: str) -> dict[str, Any]:
    return {"tag": "img", "img_key": image_key, "alt": {"tag": "plain_text", "content": alt}}


def _link_button(text: str, url: str) -> dict[str, Any]:
    return {"tag": "action", "actions": [
        {"tag": "button", "text": {"tag": "plain_text", "content": text}, "type": "primary", "url": url}]}


def _note(text: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def answer_card(answer: str, image_key: str | None = None, caption: str = "") -> dict[str, Any]:
    elements = [_md(answer or "（没有回答）")]
    if image_key:
        elements.append(_img(image_key, caption or "监控画面"))
        if caption:
            elements.append(_note(caption))
    return _card("监控助手", "blue", elements)


def alert_card(condition: str, evidence: str, time_text: str, image_key: str | None = None,
               web_url: str = "") -> dict[str, Any]:
    lines = [f"**触发条件**：{condition}", f"**时间**：{time_text}"]
    if evidence:
        lines.append(f"**依据**：{evidence}")
    elements = [_md("\n".join(lines))]
    if image_key:
        elements.append(_img(image_key, "告警时的画面"))
    elements.append(_note("回复本群 @我 可以追问，如“现在还有人吗”"))
    if web_url:
        elements.append(_link_button("查看现场", web_url))
    return _card("监控提醒", "red", elements)


def report_card(title: str, text: str, web_url: str = "") -> dict[str, Any]:
    elements = [_md(text or "这段时间没有记录。")]
    if web_url:
        elements.append(_link_button("查看现场", web_url))
    return _card(title, "green", elements)
