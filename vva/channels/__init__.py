"""接入入口：把智能体接到飞书等外部系统。每个入口是一个独立的子目录，彼此解耦。

配置（config.yaml）：
    channels:
      feishu:
        enabled: true
        ...
      my_im:                               # 自定义入口：继承 vva.channels.base.Channel
        enabled: true
        class: "path/to/my_im.py:MyChannel"
"""
from __future__ import annotations

import logging
from typing import Any

from vva.channels.base import Channel, ChannelHost
from vva.core.plugin import load_class

log = logging.getLogger(__name__)

BUILTIN = {
    "feishu": "vva.channels.feishu:FeishuChannel",
}

__all__ = ["Channel", "ChannelHost", "build_channels"]


def build_channels(cfg: dict[str, Any], host: ChannelHost) -> list[Channel]:
    channels: list[Channel] = []
    for name, c in (cfg or {}).items():
        if not isinstance(c, dict) or not c.get("enabled"):
            continue
        spec = c.get("class") or BUILTIN.get(name)
        if not spec:
            log.error("未知的接入入口：%s（自定义入口需配置 class）", name)
            continue
        try:
            channels.append(load_class(spec)(c, host))
        except Exception as e:
            log.error("接入入口 %s 创建失败：%s", name, e)
    return channels
