"""用飞书官方 SDK（lark-oapi）的长连接接收消息事件，不需要公网 IP 和回调地址。

SDK 的长连接客户端是阻塞运行的，并且使用模块级的事件循环，所以放在独立线程里、给它单独的事件循环。
收到的事件转成普通 dict 交给回调（在 SDK 线程里调用，回调需自行切回主事件循环）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)


class LongConnectionReceiver:
    def __init__(self, app_id: str, app_secret: str, on_message: Callable[[dict[str, Any]], None],
                 domain: str = "") -> None:
        self.app_id, self.app_secret, self.domain = app_id, app_secret, domain
        self.on_message = on_message
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            import lark_oapi  # noqa: F401
        except ImportError as e:
            raise RuntimeError("接收飞书消息需要安装 lark-oapi：uv pip install lark-oapi") from e
        self._thread = threading.Thread(target=self._run, name="feishu-ws", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import lark_oapi as lark
        import lark_oapi.ws.client as ws_client

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ws_client.loop = loop          # SDK 在导入时取了一个全局事件循环，这里换成本线程自己的

        def handle(data: Any) -> None:
            try:
                self.on_message(json.loads(lark.JSON.marshal(data)))
            except Exception:
                log.exception("处理飞书消息事件失败")

        handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(handle).build()
        kwargs: dict[str, Any] = {"event_handler": handler, "log_level": lark.LogLevel.WARNING}
        if self.domain:
            kwargs["domain"] = self.domain
        client = lark.ws.Client(self.app_id, self.app_secret, **kwargs)
        log.info("飞书长连接启动中")
        try:
            client.start()      # 阻塞运行，断线会自动重连
        except Exception:
            log.exception("飞书长连接异常退出")
