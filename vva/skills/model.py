"""技能的数据结构。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class RuleTemplate:
    condition: str
    cooldown_s: float = 60.0
    enabled: bool = True


@dataclass
class SkillObject:
    """技能里的特定目标：名称、别名、外观、正常/异常状态、参考图。"""
    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    normal: str = ""
    abnormal: str = ""
    images: list[str] = field(default_factory=list)   # 参考图文件名（相对于目标目录）

    @property
    def names(self) -> list[str]:
        return [self.name, *self.aliases]

    def to_text(self) -> str:
        parts = [f"【{self.name}】"]
        if self.aliases:
            parts.append(f"（也叫{'、'.join(self.aliases)}）")
        if self.description:
            parts.append(f"外观：{self.description}。")
        if self.normal:
            parts.append(f"正常：{self.normal}。")
        if self.abnormal:
            parts.append(f"异常：{self.abnormal}。")
        if self.images:
            parts.append(f"有 {len(self.images)} 张参考图。")
        return "".join(parts)


@dataclass
class Skill:
    name: str
    description: str = ""            # 一句话：什么时候用这个技能（智能体据此判断是否加载）
    body: str = ""                   # SKILL.md 正文：判断方法、标准、注意事项
    enabled: bool = True
    always: bool = False             # 常驻：正文每次都写进提示词（适合简短的现场概况）
    cameras: list[str] = field(default_factory=list)     # 只对这些摄像头生效；空表示全部
    hotwords: list[str] = field(default_factory=list)    # 语音识别热词
    rules: list[RuleTemplate] = field(default_factory=list)
    objects: dict[str, SkillObject] = field(default_factory=dict)
    dirname: str = ""                # 技能目录名
    has_tools: bool = False          # 目录里是否有 tools.py（专用工具）

    def applies_to(self, camera: str) -> bool:
        return not self.cameras or camera in self.cameras

    def to_dict(self, full: bool = True) -> dict[str, Any]:
        d = asdict(self)
        d["objects"] = [asdict(o) for o in self.objects.values()]
        if not full:
            d.pop("body")
        return d
