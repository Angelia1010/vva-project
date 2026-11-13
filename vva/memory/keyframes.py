"""关键帧图片的文件存储：<记忆目录>/frames/YYYYMMDD/HHMMSS_mmm.jpg。

数据库里只存相对路径，图片按天分目录，便于按保留期限整目录清理。
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger(__name__)


class KeyframeStore:
    def __init__(self, root: str) -> None:
        self.root = root
        os.makedirs(os.path.join(root, "frames"), exist_ok=True)

    def save(self, ts: float, jpeg: bytes) -> str:
        t = time.localtime(ts)
        rel = os.path.join("frames", time.strftime("%Y%m%d", t),
                           time.strftime("%H%M%S", t) + f"_{int((ts % 1) * 1000):03d}.jpg")
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(jpeg)
        return rel

    def load(self, rel: str) -> bytes | None:
        try:
            with open(os.path.join(self.root, rel), "rb") as f:
                return f.read()
        except OSError:
            return None

    def delete(self, rels: list[str]) -> int:
        n = 0
        dirs = set()
        for rel in rels:
            path = os.path.join(self.root, rel)
            try:
                os.remove(path)
                n += 1
            except OSError:
                pass
            dirs.add(os.path.dirname(path))
        for d in dirs:  # 当天目录删空了就一并删掉
            try:
                os.rmdir(d)
            except OSError:
                pass
        return n
