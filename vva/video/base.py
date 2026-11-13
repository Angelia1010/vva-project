"""视频源抽象接口。

任何摄像头引擎（本地摄像头、RTSP、厂商 SDK、GB28181 平台、推流服务……）
只要继承 VideoSource，实现 start()/stop()，在拿到画面时调用 self._emit(bgr_image)，
就能接入系统，其余模块完全不用改。
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Callable

import numpy as np

from vva.core.types import Frame

log = logging.getLogger(__name__)

FrameCallback = Callable[[Frame], None]


class VideoSource(ABC):
    def __init__(self, source_id: str = "cam0", **_: object) -> None:
        self.source_id = source_id
        self._callbacks: list[FrameCallback] = []
        self._running = False
        self.last_frame_ts: float = 0.0

    def on_frame(self, cb: FrameCallback) -> None:
        """注册帧回调。回调在视频源自己的线程里执行，必须尽快返回。"""
        self._callbacks.append(cb)

    def _emit(self, image: np.ndarray, ts: float | None = None) -> None:
        if image is None or image.size == 0:
            return
        if image.ndim == 2:  # 灰度图转三通道
            import cv2
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        frame = Frame(image=image, ts=ts or time.time(), source_id=self.source_id)
        self.last_frame_ts = frame.ts
        for cb in self._callbacks:
            try:
                cb(frame)
            except Exception:
                log.exception("帧回调异常")

    @property
    def running(self) -> bool:
        return self._running

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    def describe(self) -> str:
        return f"{type(self).__name__}({self.source_id})"
