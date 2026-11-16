"""一轮对话的上下文：当前会话编号、本轮产生的附件（如工具看过的画面）。

工具、事件总线在同一个 asyncio 任务里运行，用 ContextVar 传递，不需要层层传参：
  - 事件总线给推往前端的事件自动加上 session，前端只显示自己会话的内容
  - 飞书等入口把本轮附件随回答一起发送
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class Attachment:
    jpeg: bytes
    caption: str = ""


_session: ContextVar[str | None] = ContextVar("vva_session", default=None)
_attachments: ContextVar[list[Attachment] | None] = ContextVar("vva_attachments", default=None)
_turn: ContextVar[int] = ContextVar("vva_turn", default=0)
_camera: ContextVar[str | None] = ContextVar("vva_camera", default=None)
_seq = 0


def begin(session_id: str, camera: str | None = None) -> list[Attachment]:
    """在一轮对话的任务开始时调用，返回本轮的附件列表。camera：用户此刻在值守台看的那一路（没有则为 None）。"""
    global _seq
    _seq += 1
    items: list[Attachment] = []
    _turn.set(_seq)
    _session.set(session_id)
    _camera.set(camera or None)
    _attachments.set(items)
    return items


def current_session() -> str | None:
    return _session.get()


def current_camera() -> str | None:
    """本轮对话默认看的摄像头（值守台上选中的那一路）；钉钉等入口为 None。"""
    return _camera.get()


def set_camera(camera: str | None) -> None:
    _camera.set(camera or None)


def current_turn() -> int:
    """本轮对话的序号（进程内递增）；不在对话回合内为 0。用来区分“同一轮”和“下一轮”。"""
    return _turn.get()


def attach(jpeg: bytes, caption: str = "") -> None:
    """工具调用：记录一张图片；不在对话回合内时忽略。"""
    items = _attachments.get()
    if items is not None and jpeg:
        items.append(Attachment(jpeg, caption))
