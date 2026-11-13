"""基于 OpenCV 的视频源：本地摄像头、RTSP/HTTP 流、视频文件。断线自动重连。"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import cv2

from vva.video.base import VideoSource

log = logging.getLogger(__name__)

_BACKENDS = {
    "dshow": getattr(cv2, "CAP_DSHOW", None),           # Windows，打开速度快
    "msmf": getattr(cv2, "CAP_MSMF", None),             # Windows
    "avfoundation": getattr(cv2, "CAP_AVFOUNDATION", None),  # macOS
    "v4l2": getattr(cv2, "CAP_V4L2", None),             # Linux
    "ffmpeg": getattr(cv2, "CAP_FFMPEG", None),
    "gstreamer": getattr(cv2, "CAP_GSTREAMER", None),
}


class OpenCVSource(VideoSource):
    def __init__(self, uri: Any, source_id: str = "cam0", width: int | None = None,
                 height: int | None = None, fps: float | None = None,
                 loop: bool = False, backend: str | None = None,
                 reconnect_interval: float = 3.0, **kw: Any) -> None:
        super().__init__(source_id=source_id)
        self.uri = uri
        self.width, self.height, self.fps = width, height, fps
        self.loop = loop
        self.backend = _BACKENDS.get(backend) if backend else None
        self.reconnect_interval = reconnect_interval
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _open(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self.uri, self.backend) if self.backend is not None else cv2.VideoCapture(self.uri)
        if self.width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        if self.height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if self.fps:
            cap.set(cv2.CAP_PROP_FPS, self.fps)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 尽量减少缓存，降低延迟
        except Exception:
            pass
        return cap

    def _is_file(self) -> bool:
        return isinstance(self.uri, str) and os.path.isfile(self.uri)

    def _run(self) -> None:
        is_file = self._is_file()
        while not self._stop.is_set():
            cap = self._open()
            if not cap.isOpened():
                log.warning("打开视频源失败: %s，%.0f 秒后重试", self.uri, self.reconnect_interval)
                cap.release()
                self._stop.wait(self.reconnect_interval)
                continue
            log.info("视频源已打开: %s", self.uri)
            file_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            min_gap = 1.0 / self.fps if (self.fps and not is_file) else 0.0
            last = 0.0
            while not self._stop.is_set():
                ok, img = cap.read()
                if not ok:
                    if is_file and self.loop:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    break
                now = time.time()
                if min_gap and now - last < min_gap:
                    continue
                last = now
                self._emit(img, now)
                if is_file:  # 文件按原始帧率回放，模拟实时
                    time.sleep(1.0 / file_fps)
            cap.release()
            if self._stop.is_set():
                break
            if is_file and not self.loop:
                log.info("视频文件播放结束: %s", self.uri)
                break
            log.warning("视频流中断: %s，%.0f 秒后重连", self.uri, self.reconnect_interval)
            self._stop.wait(self.reconnect_interval)
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, name=f"video-{self.source_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._running = False


class LocalCameraSource(OpenCVSource):
    """电脑自带/USB 摄像头，用于本地测试。index 一般为 0。"""

    def __init__(self, index: int = 0, **kw: Any) -> None:
        super().__init__(uri=int(index), **kw)


class StreamSource(OpenCVSource):
    """RTSP / HTTP-FLV / HLS / 本地视频文件等。"""

    def __init__(self, url: str, rtsp_transport: str | None = "tcp", **kw: Any) -> None:
        if url.startswith("rtsp") and rtsp_transport:
            # 需在打开前设置；TCP 传输更稳定，避免花屏
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", f"rtsp_transport;{rtsp_transport}")
        kw.setdefault("backend", "ffmpeg" if not os.path.isfile(url) else None)
        super().__init__(uri=url, **kw)
