"""场景包的数据结构与读写。一个场景包就是一个目录：

  scenes/<场景名>/
    scene.yaml                场景说明、区域命名、语音热词、判断标准、规则模板
    objects/<目标名>/
      object.yaml             名称、别名、外观描述、正常/异常状态
      ref_01.jpg ...          参考图（建议裁好的局部，2~5 张）

只负责数据，不依赖模型和事件总线；网页管理接口和智能体工具都通过它读写。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

log = logging.getLogger(__name__)

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def safe_name(name: str) -> str:
    """把目标名变成可用作目录名的形式（保留中文）。"""
    s = _BAD_CHARS.sub("_", str(name).strip()).strip("._")
    if not s:
        raise ValueError("名称不能为空")
    return s[:60]


@dataclass
class RuleTemplate:
    condition: str
    cooldown_s: float = 60.0
    enabled: bool = True


@dataclass
class SceneObject:
    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""            # 外观特征
    normal: str = ""                 # 正常状态是什么样
    abnormal: str = ""               # 异常状态是什么样
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
class Scene:
    name: str = "default"
    description: str = ""                                   # 场景概况
    areas: dict[str, str] = field(default_factory=dict)     # 画面区域命名，如 {"左侧": "3 号货架"}
    hotwords: list[str] = field(default_factory=list)       # 专业术语，提高语音识别准确率
    standards: list[str] = field(default_factory=list)      # 判断标准、阈值，如“压力表正常 0.2~0.5MPa”
    rules: list[RuleTemplate] = field(default_factory=list)  # 启动时自动加载的监控规则
    objects: dict[str, SceneObject] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["objects"] = [asdict(o) for o in self.objects.values()]
        return d


class SceneStore:
    def __init__(self, root: str) -> None:
        self.root = root
        self._lock = threading.Lock()
        os.makedirs(os.path.join(root, "objects"), exist_ok=True)
        self.scene = self._load()

    # ---------------------------------------------------------------- 读
    def _load(self) -> Scene:
        path = os.path.join(self.root, "scene.yaml")
        data: dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        scene = Scene(
            name=str(data.get("name") or os.path.basename(os.path.abspath(self.root))),
            description=str(data.get("description") or ""),
            areas={str(k): str(v) for k, v in (data.get("areas") or {}).items()},
            hotwords=[str(x) for x in data.get("hotwords") or []],
            standards=[str(x) for x in data.get("standards") or []],
            rules=[RuleTemplate(condition=str(r["condition"]), cooldown_s=float(r.get("cooldown_s", 60)),
                                enabled=bool(r.get("enabled", True)))
                   for r in data.get("rules") or [] if isinstance(r, dict) and r.get("condition")],
        )
        obj_root = os.path.join(self.root, "objects")
        for d in sorted(os.listdir(obj_root)):
            meta = os.path.join(obj_root, d, "object.yaml")
            if not os.path.isfile(meta):
                continue
            with open(meta, encoding="utf-8") as f:
                o = yaml.safe_load(f) or {}
            images = sorted(x for x in os.listdir(os.path.join(obj_root, d))
                            if x.lower().endswith((".jpg", ".jpeg", ".png")))
            obj = SceneObject(name=str(o.get("name") or d), aliases=[str(a) for a in o.get("aliases") or []],
                              description=str(o.get("description") or ""), normal=str(o.get("normal") or ""),
                              abnormal=str(o.get("abnormal") or ""), images=images)
            scene.objects[obj.name] = obj
        log.info("已加载场景包 %s：%d 个目标", scene.name, len(scene.objects))
        return scene

    def reload(self) -> Scene:
        with self._lock:
            self.scene = self._load()
        return self.scene

    def object_dir(self, name: str) -> str:
        return os.path.join(self.root, "objects", safe_name(name))

    def image_path(self, name: str, filename: str) -> str:
        if os.path.basename(filename) != filename:
            raise ValueError("文件名不合法")
        return os.path.join(self.object_dir(name), filename)

    def read_image(self, name: str, filename: str) -> bytes | None:
        try:
            with open(self.image_path(name, filename), "rb") as f:
                return f.read()
        except (OSError, ValueError):
            return None

    # ---------------------------------------------------------------- 写
    def save_scene(self, **fields: Any) -> Scene:
        """更新场景级字段（description / areas / hotwords / standards / rules）。"""
        with self._lock:
            s = self.scene
            for k in ("description", "areas", "hotwords", "standards"):
                if k in fields and fields[k] is not None:
                    setattr(s, k, fields[k])
            if fields.get("rules") is not None:
                s.rules = [r if isinstance(r, RuleTemplate) else RuleTemplate(**r) for r in fields["rules"]]
            data = {"name": s.name, "description": s.description, "areas": s.areas, "hotwords": s.hotwords,
                    "standards": s.standards, "rules": [asdict(r) for r in s.rules]}
            with open(os.path.join(self.root, "scene.yaml"), "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        return self.scene

    def save_object(self, name: str, aliases: list[str] | None = None, description: str = "",
                    normal: str = "", abnormal: str = "") -> SceneObject:
        d = self.object_dir(name)
        os.makedirs(d, exist_ok=True)
        data = {"name": name.strip(), "aliases": [a.strip() for a in aliases or [] if a.strip()],
                "description": description.strip(), "normal": normal.strip(), "abnormal": abnormal.strip()}
        with open(os.path.join(d, "object.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        self.reload()
        return self.scene.objects[data["name"]]

    def delete_object(self, name: str) -> bool:
        d = self.object_dir(name)
        if not os.path.isdir(d):
            return False
        shutil.rmtree(d)
        self.reload()
        return True

    def add_image(self, name: str, jpeg: bytes) -> str:
        if name not in self.scene.objects:
            raise KeyError(f"没有这个目标：{name}")
        d = self.object_dir(name)
        n = 1
        while os.path.exists(os.path.join(d, f"ref_{n:02d}.jpg")):
            n += 1
        filename = f"ref_{n:02d}.jpg"
        with open(os.path.join(d, filename), "wb") as f:
            f.write(jpeg)
        self.reload()
        return filename

    def delete_image(self, name: str, filename: str) -> bool:
        try:
            os.remove(self.image_path(name, filename))
        except (OSError, ValueError):
            return False
        self.reload()
        return True
