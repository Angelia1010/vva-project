"""根据配置创建视频源。新增引擎只需写一个 VideoSource 子类，并在配置里指定 custom.class。"""
from __future__ import annotations

from typing import Any

from vva.core.plugin import load_class
from vva.video.base import VideoSource


def create_video_source(cfg: dict[str, Any]) -> VideoSource:
    vtype = (cfg.get("type") or "local").lower()
    sid = cfg.get("source_id", "cam0")
    if vtype == "local":
        from vva.video.opencv_source import LocalCameraSource
        return LocalCameraSource(source_id=sid, **cfg.get("local", {}))
    if vtype == "stream":
        from vva.video.opencv_source import StreamSource
        params = dict(cfg.get("stream", {}))
        if not params.get("url"):
            raise ValueError("video.type=stream 时必须配置 video.stream.url")
        return StreamSource(source_id=sid, **params)
    if vtype == "push":
        from vva.video.push_source import PushVideoSource
        return PushVideoSource(source_id=sid, **cfg.get("push", {}))
    if vtype == "custom":
        custom = cfg.get("custom", {})
        cls = load_class(custom["class"])
        src = cls(source_id=sid, **(custom.get("params") or {}))
        if not isinstance(src, VideoSource):
            raise TypeError(f"{custom['class']} 必须继承 vva.video.base.VideoSource")
        return src
    raise ValueError(f"未知的 video.type: {vtype}（可选 local / stream / push / custom）")
