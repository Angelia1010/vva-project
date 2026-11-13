"""推送式视频源：由外部主动把画面送进来。

两种用法：
1. 同进程：在厂商 SDK 的帧回调里调用 push_frame(bgr_image) 或 push_jpeg(bytes)。
2. 跨进程/跨语言：配置 video.type=push，外部程序向
   POST http://<host>:<port>/api/ingest/frame  （请求体为 JPEG 字节）推帧。
"""
from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from vva.video.base import VideoSource


class PushVideoSource(VideoSource):
    def __init__(self, source_id: str = "cam0", **kw: Any) -> None:
        super().__init__(source_id=source_id)

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def push_frame(self, image: np.ndarray, ts: float | None = None) -> None:
        if self._running:
            self._emit(image, ts)

    def push_jpeg(self, data: bytes, ts: float | None = None) -> bool:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return False
        self.push_frame(img, ts)
        return True
