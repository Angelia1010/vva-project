"""模块间共享的数据类型。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Frame:
    """一帧画面。image 为 OpenCV 的 BGR uint8 数组。"""
    image: np.ndarray
    ts: float = field(default_factory=time.time)
    source_id: str = "cam0"

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])


def fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))
