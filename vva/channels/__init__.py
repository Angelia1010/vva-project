"""接入入口：把智能体接到飞书、钉钉等外部系统。每个入口是一个独立的子目录，彼此解耦。

  chat.py      飞书、钉钉这类聊天机器人入口的公共部分（权限、指令、告警推送、定时报告）
  feishu/      飞书
  dingtalk/    钉钉

配置（config.yaml），网页 /feishu、/dingtalk 页面保存的配置会覆盖这里的同名项（存于 data/channels/）：
    channels:
      feishu:
        enabled: true
        ...
      dingtalk:
        enabled: true
        ...
      my_im:                               # 自定义入口：继承 vva.channels.base.Channel
        enabled: true
        class: "path/to/my_im.py:MyChannel"
"""
from __future__ import annotations

from typing import Any

from vva.channels.base import Channel, ChannelHost
from vva.channels.manager import BUILTIN, ChannelManager, ChannelSettings

__all__ = ["Channel", "ChannelHost", "ChannelManager", "ChannelSettings", "BUILTIN", "build_channels"]


def build_channels(cfg: dict[str, Any], host: ChannelHost, settings_dir: str = "") -> list[Channel]:
    """按配置创建已启用的入口（不读取网页保存的配置时，settings_dir 留空）。"""
    mgr = ChannelManager(cfg, host, settings_dir or "/nonexistent-vva-settings")
    return list(mgr.channels.values())
