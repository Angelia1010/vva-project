"""接入入口的管理：按配置创建、启停入口；网页修改的配置保存在 data/channels/<入口>.yaml，覆盖 config.yaml 中的同名项。

配置变化时尽量直接生效；连接参数变化（如飞书的 app_id、钉钉的 Client ID）需要重启服务，由 restart_required 告知调用方。
"""
from __future__ import annotations

import copy
import logging
import os
from typing import Any

import yaml

from vva.channels.base import Channel, ChannelHost
from vva.core.plugin import load_class

log = logging.getLogger(__name__)

BUILTIN = {
    "feishu": "vva.channels.feishu:FeishuChannel",
    "dingtalk": "vva.channels.dingtalk:DingTalkChannel",
}


class ChannelSettings:
    """网页保存的入口配置（YAML 文件，权限 600，因为里面有密钥）。"""

    def __init__(self, root: str) -> None:
        self.root = root

    def path(self, name: str) -> str:
        return os.path.join(self.root, f"{name}.yaml")

    def load(self, name: str) -> dict[str, Any]:
        try:
            with open(self.path(name), encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}

    def save(self, name: str, data: dict[str, Any]) -> None:
        os.makedirs(self.root, exist_ok=True)
        path = self.path(name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


class ChannelManager:
    def __init__(self, cfg: dict[str, Any], host: ChannelHost, settings_dir: str = "data/channels") -> None:
        self.base = copy.deepcopy(cfg or {})
        self.host = host
        self.settings = ChannelSettings(settings_dir)
        self.channels: dict[str, Channel] = {}
        self._started = False
        self._opened: set[str] = set()      # 本进程里启动过的入口（长连接线程无法彻底关闭，再次开启需重启服务）
        for name in self.names():
            c = self.config(name)
            if c.get("enabled"):
                ch = self._build(name, c)
                if ch:
                    self.channels[name] = ch

    # ---------------------------------------------------------------- 配置
    def names(self) -> list[str]:
        return list(dict.fromkeys([*self.base.keys(), *BUILTIN.keys()]))

    def config(self, name: str) -> dict[str, Any]:
        """config.yaml 的配置，再叠加网页保存的配置。"""
        return {**(self.base.get(name) or {}), **self.settings.load(name)}

    def _build(self, name: str, cfg: dict[str, Any]) -> Channel | None:
        spec = cfg.get("class") or BUILTIN.get(name)
        if not spec:
            log.error("未知的接入入口：%s（自定义入口需配置 class）", name)
            return None
        try:
            return load_class(spec)(cfg, self.host)
        except Exception as e:
            log.error("接入入口 %s 创建失败：%s", name, e)
            return None

    # ---------------------------------------------------------------- 启停
    async def start(self) -> None:
        self._started = True
        for name, ch in self.channels.items():
            self._opened.add(name)
            try:
                await ch.start()
            except Exception as e:
                log.error("接入入口 %s 启动失败：%s", ch.name, e)

    async def stop(self) -> None:
        for ch in self.channels.values():
            try:
                await ch.stop()
            except Exception as e:
                log.warning("接入入口 %s 停止失败：%s", ch.name, e)

    def get(self, name: str) -> Channel | None:
        return self.channels.get(name)

    def __iter__(self):
        return iter(self.channels.values())

    async def reconfigure(self, name: str, changes: dict[str, Any]) -> bool:
        """保存网页修改的配置并生效。返回 True 表示需要重启服务才能完全生效。"""
        saved = {**self.settings.load(name), **changes}
        self.settings.save(name, saved)
        cfg = self.config(name)
        ch = self.channels.get(name)
        if ch is not None and cfg.get("enabled"):
            return ch.apply(cfg) if hasattr(ch, "apply") else True
        if ch is not None:                      # 关闭
            await ch.stop()
            del self.channels[name]
            return False
        if cfg.get("enabled"):                  # 新开启
            if name in self._opened:            # 之前开过又关了：旧的长连接还在，重启服务后再开
                return True
            new = self._build(name, cfg)
            if new is None:
                return False
            self.channels[name] = new
            if self._started:
                self._opened.add(name)
                await new.start()
        return False
