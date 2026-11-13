"""技能目录的读写。每个技能一个目录：

  skills/<技能名>/
    SKILL.md                  开头 YAML（名称、用途描述、开关、热词、规则模板）+ 正文（判断方法、标准）
    objects/<目标名>/
      object.yaml             名称、别名、外观、正常/异常状态
      ref_01.jpg ...          参考图
    tools.py                  可选：专用工具（只能在服务器上放置，网页不能上传代码）

只负责数据，不依赖模型和事件总线。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from typing import Any

import yaml

from vva.skills.model import RuleTemplate, Skill, SkillObject

log = logging.getLogger(__name__)

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]+')
_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
IMAGE_EXT = (".jpg", ".jpeg", ".png")


def safe_name(name: str) -> str:
    """把名称变成可用作目录名的形式（保留中文）。"""
    s = _BAD_CHARS.sub("_", str(name).strip()).strip("._")
    if not s:
        raise ValueError("名称不能为空")
    return s[:60]


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONT_RE.match(text or "")
    if not m:
        return {}, (text or "").strip()
    meta = yaml.safe_load(m.group(1)) or {}
    return (meta if isinstance(meta, dict) else {}), m.group(2).strip()


def render_skill_md(skill: Skill) -> str:
    meta: dict[str, Any] = {"name": skill.name, "description": skill.description, "enabled": skill.enabled}
    if skill.always:
        meta["always"] = True
    if skill.cameras:
        meta["cameras"] = skill.cameras
    if skill.hotwords:
        meta["hotwords"] = skill.hotwords
    if skill.rules:
        meta["rules"] = [r.__dict__ for r in skill.rules]
    head = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{head}\n---\n\n{skill.body.strip()}\n"


def _rules(items: Any) -> list[RuleTemplate]:
    out = []
    for r in items or []:
        if isinstance(r, RuleTemplate):
            out.append(r)
        elif isinstance(r, dict) and r.get("condition"):
            out.append(RuleTemplate(str(r["condition"]), float(r.get("cooldown_s", 60)), bool(r.get("enabled", True))))
    return out


class SkillStore:
    def __init__(self, root: str) -> None:
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._lock = threading.Lock()
        self.skills: dict[str, Skill] = {}
        self.reload()

    # ---------------------------------------------------------------- 读
    def _load_objects(self, skill_dir: str) -> dict[str, SkillObject]:
        objs: dict[str, SkillObject] = {}
        root = os.path.join(skill_dir, "objects")
        if not os.path.isdir(root):
            return objs
        for d in sorted(os.listdir(root)):
            meta = os.path.join(root, d, "object.yaml")
            if not os.path.isfile(meta):
                continue
            with open(meta, encoding="utf-8") as f:
                o = yaml.safe_load(f) or {}
            images = sorted(x for x in os.listdir(os.path.join(root, d)) if x.lower().endswith(IMAGE_EXT))
            obj = SkillObject(name=str(o.get("name") or d), aliases=[str(a) for a in o.get("aliases") or []],
                              description=str(o.get("description") or ""), normal=str(o.get("normal") or ""),
                              abnormal=str(o.get("abnormal") or ""), images=images)
            objs[obj.name] = obj
        return objs

    def _load_one(self, dirname: str) -> Skill | None:
        d = os.path.join(self.root, dirname)
        md = os.path.join(d, "SKILL.md")
        if not os.path.isfile(md):
            return None
        with open(md, encoding="utf-8") as f:
            meta, body = parse_skill_md(f.read())
        return Skill(
            name=str(meta.get("name") or dirname), description=str(meta.get("description") or ""), body=body,
            enabled=bool(meta.get("enabled", True)), always=bool(meta.get("always", False)),
            cameras=[str(c) for c in meta.get("cameras") or []], hotwords=[str(h) for h in meta.get("hotwords") or []],
            rules=_rules(meta.get("rules")), objects=self._load_objects(d), dirname=dirname,
            has_tools=os.path.isfile(os.path.join(d, "tools.py")))

    def reload(self) -> dict[str, Skill]:
        with self._lock:
            skills: dict[str, Skill] = {}
            for dirname in sorted(os.listdir(self.root)):
                try:
                    s = self._load_one(dirname)
                except Exception as e:
                    log.warning("技能 %s 读取失败：%s", dirname, e)
                    continue
                if s:
                    skills[s.name] = s
            self.skills = skills
        return self.skills

    def get(self, name: str) -> Skill:
        s = self.skills.get(name)
        if s is None:
            raise KeyError(f"没有这个技能：{name}")
        return s

    def skill_dir(self, name: str) -> str:
        return os.path.join(self.root, self.get(name).dirname)

    def object_dir(self, skill: str, obj: str) -> str:
        return os.path.join(self.skill_dir(skill), "objects", safe_name(obj))

    def read_image(self, skill: str, obj: str, filename: str) -> bytes | None:
        if os.path.basename(filename) != filename:
            return None
        try:
            with open(os.path.join(self.object_dir(skill, obj), filename), "rb") as f:
                return f.read()
        except (OSError, KeyError, ValueError):
            return None

    # ---------------------------------------------------------------- 技能
    def _write(self, skill: Skill) -> None:
        d = os.path.join(self.root, skill.dirname)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(render_skill_md(skill))

    def create(self, name: str, description: str = "", body: str = "", **fields: Any) -> Skill:
        name = name.strip()
        if name in self.skills:
            raise ValueError(f"技能“{name}”已存在")
        dirname = safe_name(name)
        if os.path.exists(os.path.join(self.root, dirname)):
            raise ValueError(f"目录 {dirname} 已存在")
        self._write(Skill(name=name, description=description.strip(), body=body, dirname=dirname,
                          **{k: v for k, v in fields.items() if k in ("enabled", "always", "cameras", "hotwords")},
                          rules=_rules(fields.get("rules"))))
        self.reload()
        return self.get(name)

    def update(self, name: str, **fields: Any) -> Skill:
        """更新 description / body / enabled / always / cameras / hotwords / rules。名称不能改。"""
        s = self.get(name)
        for k in ("description", "body", "enabled", "always", "cameras", "hotwords"):
            if fields.get(k) is not None:
                setattr(s, k, fields[k])
        if fields.get("rules") is not None:
            s.rules = _rules(fields["rules"])
        self._write(s)
        self.reload()
        return self.get(name)

    def delete(self, name: str) -> bool:
        if name not in self.skills:
            return False
        shutil.rmtree(self.skill_dir(name))
        self.reload()
        return True

    # ---------------------------------------------------------------- 目标与参考图
    def save_object(self, skill: str, name: str, aliases: list[str] | None = None, description: str = "",
                    normal: str = "", abnormal: str = "") -> SkillObject:
        d = self.object_dir(skill, name)
        os.makedirs(d, exist_ok=True)
        data = {"name": name.strip(), "aliases": [a.strip() for a in aliases or [] if a.strip()],
                "description": description.strip(), "normal": normal.strip(), "abnormal": abnormal.strip()}
        with open(os.path.join(d, "object.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        self.reload()
        return self.get(skill).objects[data["name"]]

    def delete_object(self, skill: str, name: str) -> bool:
        d = self.object_dir(skill, name)
        if not os.path.isdir(d):
            return False
        shutil.rmtree(d)
        self.reload()
        return True

    def add_image(self, skill: str, obj: str, jpeg: bytes) -> str:
        if obj not in self.get(skill).objects:
            raise KeyError(f"没有这个目标：{obj}")
        d = self.object_dir(skill, obj)
        n = 1
        while os.path.exists(os.path.join(d, f"ref_{n:02d}.jpg")):
            n += 1
        filename = f"ref_{n:02d}.jpg"
        with open(os.path.join(d, filename), "wb") as f:
            f.write(jpeg)
        self.reload()
        return filename

    def delete_image(self, skill: str, obj: str, filename: str) -> bool:
        if os.path.basename(filename) != filename:
            return False
        try:
            os.remove(os.path.join(self.object_dir(skill, obj), filename))
        except (OSError, KeyError, ValueError):
            return False
        self.reload()
        return True

    # ---------------------------------------------------------------- 迁移
    def migrate_scene(self, scene_dir: str, name: str = "现场概况") -> Skill | None:
        """把旧版场景包（scene.yaml + objects/）转换成一个常驻技能。原目录保留不动。"""
        path = os.path.join(scene_dir, "scene.yaml")
        marker = os.path.join(self.root, ".migrated_scene")
        if os.path.exists(marker) or name in self.skills or not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        lines = []
        if data.get("description"):
            lines.append(str(data["description"]))
        if data.get("areas"):
            lines.append("画面区域：\n" + "\n".join(f"- {k}：{v}" for k, v in data["areas"].items()))
        if data.get("standards"):
            lines.append("判断标准：\n" + "\n".join(f"- {x}" for x in data["standards"]))
        objects_src = os.path.join(scene_dir, "objects")
        has_objects = os.path.isdir(objects_src) and any(os.scandir(objects_src))
        if not lines and not has_objects and not data.get("rules"):
            return None     # 旧场景包是空的，不需要转换
        self.create(name, "现场的基本情况、区域划分和判断标准，常驻生效", "\n\n".join(lines), always=True,
                            hotwords=data.get("hotwords") or [], rules=data.get("rules") or [])
        if has_objects:
            shutil.copytree(objects_src, os.path.join(self.skill_dir(name), "objects"), dirs_exist_ok=True)
            self.reload()
        with open(marker, "w", encoding="utf-8") as f:
            f.write(os.path.abspath(scene_dir) + "\n")
        log.info("已把旧版场景包 %s 转换为技能“%s”", scene_dir, name)
        return self.get(name)
