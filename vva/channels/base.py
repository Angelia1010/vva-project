"""接入入口的公共接口。

入口（飞书、企业微信、钉钉……）只做两件事：把外部消息交给智能体、把回答和告警发回外部。
它们只依赖 ChannelHost 这组方法，不直接接触智能体、工具和模型，新增入口不需要改核心代码。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

from vva.core.bus import EventBus


class ChannelHost(Protocol):
    """应用提供给入口的能力（由 vva.app.App 实现）。"""

    bus: EventBus

    async def ask(self, text: str, session: str, source: str = "api", readonly: bool = False) -> Any:
        """问一个问题，返回 Reply(text, attachments, interrupted, error)。同一会话的新问题会打断旧问题。"""

    def add_note(self, session: str, text: str) -> None:
        """把系统主动发出的内容（如告警）记进会话，之后可以接着追问。"""

    def reset_session(self, session: str) -> None:
        """清空会话上下文。"""

    def snapshot(self, max_side: int = 1280) -> bytes | None:
        """当前画面的 JPEG。"""

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str | None:
        """语音转文字；没有配置语音识别时返回 None。"""

    async def report(self, start: float, end: float) -> str:
        """生成一段时间的监控报告（基于长期记忆）。"""


class Channel(ABC):
    """一个接入入口。构造时只保存配置，start() 时才连接外部服务。"""

    name: str = "channel"

    def __init__(self, cfg: dict[str, Any], host: ChannelHost) -> None:
        self.cfg = cfg
        self.host = host

    @abstractmethod
    async def start(self) -> None:
        """连接外部服务并开始收发；连接失败只记日志，不影响主服务。"""

    async def stop(self) -> None:  # noqa: B027  默认无需清理，子类按需覆盖
        """断开连接。"""
