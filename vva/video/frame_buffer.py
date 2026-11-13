"""帧缓存：保存最近 N 秒的 JPEG 帧，供“回看”“看一段时间的变化”使用。

- 最新一帧保留原始图像，保证“看当前”拿到的是最新画面；
- 历史帧按 buffer.fps 抽样并压缩成 JPEG，60 秒 × 2 帧/秒只占几十 MB。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from vva.core.types import Frame
from vva.perception.imaging import encode_jpeg, resize_max_side


@dataclass
class BufferedFrame:
    ts: float
    jpeg: bytes
    width: int
    height: int


class FrameBuffer:
    def __init__(self, seconds: float = 60, fps: float = 2.0, jpeg_quality: int = 85,
                 max_side: int = 1920) -> None:
        self.seconds = float(seconds)
        self.interval = 1.0 / max(float(fps), 0.1)
        self.quality = int(jpeg_quality)
        self.max_side = int(max_side)
        self._frames: deque[BufferedFrame] = deque()
        self._latest: Frame | None = None
        self._last_store = 0.0
        self._lock = threading.Lock()
        self._preview_cache: tuple[float, int, bytes] | None = None

    # ---- 写入（视频线程） ----
    def add(self, frame: Frame) -> None:
        with self._lock:
            self._latest = frame
            need_store = frame.ts - self._last_store >= self.interval
            if need_store:
                self._last_store = frame.ts
        if not need_store:
            return
        img = resize_max_side(frame.image, self.max_side)
        bf = BufferedFrame(ts=frame.ts, jpeg=encode_jpeg(img, self.quality),
                           width=img.shape[1], height=img.shape[0])
        with self._lock:
            self._frames.append(bf)
            cutoff = frame.ts - self.seconds
            while self._frames and self._frames[0].ts < cutoff:
                self._frames.popleft()

    # ---- 读取 ----
    def latest_frame(self) -> Frame | None:
        with self._lock:
            return self._latest

    def latest_snapshot(self, max_side: int | None = None, quality: int | None = None) -> BufferedFrame | None:
        f = self.latest_frame()
        if f is None:
            return None
        img = resize_max_side(f.image, max_side or self.max_side)
        return BufferedFrame(ts=f.ts, jpeg=encode_jpeg(img, quality or self.quality),
                             width=img.shape[1], height=img.shape[0])

    def preview_jpeg(self, max_side: int = 960, quality: int = 70) -> tuple[float, bytes] | None:
        """给网页 MJPEG 预览用，同一帧只编码一次。"""
        f = self.latest_frame()
        if f is None:
            return None
        cache = self._preview_cache
        if cache and cache[0] == f.ts and cache[1] == max_side:
            return cache[0], cache[2]
        data = encode_jpeg(resize_max_side(f.image, max_side), quality)
        self._preview_cache = (f.ts, max_side, data)
        return f.ts, data

    def at(self, seconds_ago: float) -> BufferedFrame | None:
        """返回最接近“现在 - seconds_ago”的缓存帧。"""
        target = time.time() - max(0.0, float(seconds_ago))
        with self._lock:
            if not self._frames:
                return None
            return min(self._frames, key=lambda b: abs(b.ts - target))

    def range(self, seconds: float, n: int) -> list[BufferedFrame]:
        """返回最近 seconds 秒内均匀抽取的 n 帧（按时间先后）。"""
        n = max(1, int(n))
        cutoff = time.time() - max(0.0, float(seconds))
        with self._lock:
            frames = [b for b in self._frames if b.ts >= cutoff]
        if len(frames) <= n:
            return frames
        step = (len(frames) - 1) / (n - 1) if n > 1 else 0
        return [frames[round(i * step)] for i in range(n)] if n > 1 else [frames[-1]]

    def stats(self) -> dict:
        with self._lock:
            return {
                "buffered_frames": len(self._frames),
                "oldest_ts": self._frames[0].ts if self._frames else None,
                "latest_ts": self._latest.ts if self._latest else None,
            }
