"""图像小工具：编码、缩放、按归一化坐标裁剪放大、坐标格式转换。"""
from __future__ import annotations

from typing import Sequence

import cv2
import numpy as np


def encode_jpeg(img: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return buf.tobytes()


def decode_jpeg(data: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("JPEG 解码失败")
    return img


def resize_max_side(img: np.ndarray, max_side: int | None) -> np.ndarray:
    if not max_side:
        return img
    h, w = img.shape[:2]
    s = max(h, w)
    if s <= max_side:
        return img
    r = max_side / s
    return cv2.resize(img, (max(1, int(w * r)), max(1, int(h * r))), interpolation=cv2.INTER_AREA)


def validate_region(region: Sequence[float] | None) -> list[float] | None:
    """region = [x1, y1, x2, y2]，取值 0~1，左上角为原点。不合法返回 None。"""
    if not region or len(region) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in region)
    except (TypeError, ValueError):
        return None
    # 兼容模型误传 0~1000 的情况
    if max(x1, y1, x2, y2) > 1.0 + 1e-6:
        x1, y1, x2, y2 = (v / 1000.0 for v in (x1, y1, x2, y2))
    x1, x2 = sorted((min(max(x1, 0.0), 1.0), min(max(x2, 0.0), 1.0)))
    y1, y2 = sorted((min(max(y1, 0.0), 1.0), min(max(y2, 0.0), 1.0)))
    if x2 - x1 < 0.01 or y2 - y1 < 0.01:
        return None
    return [x1, y1, x2, y2]


def crop_region(img: np.ndarray, region: Sequence[float], pad: float = 0.03,
                min_side: int = 768) -> np.ndarray:
    """按归一化坐标裁剪（四周留一点余量），小区域放大到 min_side，便于大模型看清细节。"""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = region
    x1, y1 = max(0.0, x1 - pad), max(0.0, y1 - pad)
    x2, y2 = min(1.0, x2 + pad), min(1.0, y2 + pad)
    crop = img[int(y1 * h):max(int(y2 * h), int(y1 * h) + 1), int(x1 * w):max(int(x2 * w), int(x1 * w) + 1)]
    ch, cw = crop.shape[:2]
    s = max(ch, cw)
    if 0 < s < min_side:
        r = min_side / s
        crop = cv2.resize(crop, (round(cw * r), round(ch * r)), interpolation=cv2.INTER_CUBIC)
    return crop


def box_to_normalized(box: Sequence[float], fmt: str, img_w: int, img_h: int) -> list[float] | None:
    """把大模型返回的框统一转成 0~1 坐标。

    fmt:
      "0-1000"  框坐标为 0~1000 的相对值（Qwen3-VL、Gemini 等常见）
      "0-1"     框坐标为 0~1 的相对值
      "pixel"   框坐标为送入图片的像素坐标（如 Qwen2.5-VL）
    """
    if not box or len(box) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if fmt == "pixel":
        x1, x2 = x1 / img_w, x2 / img_w
        y1, y2 = y1 / img_h, y2 / img_h
    elif fmt == "0-1000":
        x1, y1, x2, y2 = (v / 1000.0 for v in (x1, y1, x2, y2))
    return validate_region([x1, y1, x2, y2])
