"""用钉钉官方 SDK（dingtalk-stream）的 Stream 模式接收机器人消息，不需要公网 IP 和回调地址。

SDK 的客户端阻塞运行（自带断线重连），放在独立线程里、给它单独的事件循环。
收到的消息（dict）交给回调（在 SDK 线程里调用，回调需自行切回主事件循环）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

BOT_TOPIC = "/v1.0/im/bot/messages/get"


class StreamReceiver:
    def __init__(self, client_id: str, client_secret: str, on_message: Callable[[dict[str, Any]], None]) -> None:
        self.client_id, self.client_secret = client_id, client_secret
        self.on_message = on_message
        self._thread: threading.Thread | None = None
        self.state = "not_started"       # not_started | running | error
        self.error = ""
        self.last_event = 0.0

    def start(self) -> None:
        try:
            import dingtalk_stream  # noqa: F401
        except ImportError as e:
            raise RuntimeError("接收钉钉消息需要安装 dingtalk-stream：uv pip install dingtalk-stream") from e
        self._thread = threading.Thread(target=self._run, name="dingtalk-stream", daemon=True)
        self._thread.start()

    def _handle(self, data: Any) -> None:
        self.last_event = time.time()
        try:
            self.on_message(data if isinstance(data, dict) else {})
        except Exception:
            log.exception("处理钉钉消息失败")

    def _run(self) -> None:
        import dingtalk_stream

        receiver = self

        class Handler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback: Any):     # SDK 约定的签名
                receiver._handle(callback.data)
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        asyncio.set_event_loop(asyncio.new_event_loop())
        client = dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential(self.client_id, self.client_secret))
        client.register_callback_handler(BOT_TOPIC, Handler())
        log.info("钉钉 Stream 连接启动中")
        self.state = "running"
        try:
            client.start_forever()      # 阻塞运行，断线会自动重连
        except Exception as e:
            log.exception("钉钉 Stream 连接异常退出")
            self.state, self.error = "error", str(e)
