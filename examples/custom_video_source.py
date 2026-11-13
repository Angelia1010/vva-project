"""自定义视频源示例。

配置方法（config.yaml）：
    video:
      type: custom
      custom:
        class: "examples/custom_video_source.py:TestPatternSource"
        params: {fps: 10}

写自己的引擎时：继承 VideoSource，实现 start()/stop()，拿到画面后调用 self._emit(bgr_image)。
"""
from __future__ import annotations

import math
import threading
import time

import cv2
import numpy as np

from vva.video.base import VideoSource


class TestPatternSource(VideoSource):
    """无需摄像头的测试画面：一块指针表盘 + 当前时间，指针缓慢摆动。可用来测试“读表”。"""

    def __init__(self, source_id: str = "cam0", fps: float = 10, width: int = 1280, height: int = 720, **kw):
        super().__init__(source_id=source_id)
        self.fps, self.w, self.h = fps, width, height
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _draw(self, t: float) -> np.ndarray:
        img = np.full((self.h, self.w, 3), 235, np.uint8)
        cx, cy, r = self.w // 2, self.h // 2 + 20, min(self.w, self.h) // 3
        cv2.circle(img, (cx, cy), r, (255, 255, 255), -1)
        cv2.circle(img, (cx, cy), r, (40, 40, 40), 4)
        for v in range(0, 101, 10):  # 刻度 0~100，覆盖 270 度
            a = math.radians(135 + v * 2.7)
            p1 = (int(cx + (r - 25) * math.cos(a)), int(cy + (r - 25) * math.sin(a)))
            p2 = (int(cx + (r - 5) * math.cos(a)), int(cy + (r - 5) * math.sin(a)))
            cv2.line(img, p1, p2, (30, 30, 30), 3)
            tp = (int(cx + (r - 55) * math.cos(a)) - 14, int(cy + (r - 55) * math.sin(a)) + 8)
            cv2.putText(img, str(v), tp, cv2.FONT_HERSHEY_SIMPLEX, 0.7, (30, 30, 30), 2)
        value = 50 + 40 * math.sin(t / 20)  # 指针在 10~90 之间缓慢摆动
        a = math.radians(135 + value * 2.7)
        cv2.line(img, (cx, cy), (int(cx + (r - 40) * math.cos(a)), int(cy + (r - 40) * math.sin(a))), (0, 0, 200), 6)
        cv2.circle(img, (cx, cy), 12, (0, 0, 200), -1)
        cv2.putText(img, "PRESSURE  MPa x0.01", (cx - 130, cy + r // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (60, 60, 60), 2)
        cv2.putText(img, time.strftime("%Y-%m-%d %H:%M:%S"), (30, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (20, 20, 20), 2)
        return img

    def _run(self) -> None:
        t0 = time.time()
        while not self._stop.is_set():
            self._emit(self._draw(time.time() - t0))
            self._stop.wait(1.0 / self.fps)

    def start(self) -> None:
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._running = False


class VendorSDKSource(VideoSource):
    """厂商 SDK 接入模板（伪代码，按你的 SDK 修改）。

    大多数 SDK 的流程：初始化 → 登录设备 → 开启实时预览并注册解码后回调 → 回调里拿到 YUV/RGB 帧。
    只需在回调中把帧转成 BGR 的 numpy 数组，然后调用 self._emit(image)。
    """

    def __init__(self, source_id: str = "cam0", ip: str = "", port: int = 8000,
                 user: str = "admin", password: str = "", channel: int = 1, **kw):
        super().__init__(source_id=source_id)
        self.ip, self.port, self.user, self.password, self.channel = ip, port, user, password, channel
        self._handle = None

    def _on_decoded_frame(self, yuv_bytes: bytes, width: int, height: int) -> None:
        # 示例：I420(YUV420P) 转 BGR；不同 SDK 的像素格式不同，按实际修改
        yuv = np.frombuffer(yuv_bytes, np.uint8).reshape((height * 3 // 2, width))
        bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
        self._emit(bgr)

    def start(self) -> None:
        # sdk.init()
        # login = sdk.login(self.ip, self.port, self.user, self.password)
        # self._handle = sdk.real_play(login, self.channel, callback=self._on_decoded_frame)
        raise NotImplementedError("请按你的摄像头 SDK 实现 start()")

    def stop(self) -> None:
        # sdk.stop_real_play(self._handle); sdk.logout(); sdk.cleanup()
        self._running = False
