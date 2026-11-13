"""技能管理：按需加载（渐进式披露）。

- 智能体平时只看到已启用技能的“名称 + 一句话用途 + 目标名称”；常驻技能（always）的正文每次都带上
- 用得上时调用 load_skill 读取正文和目标说明；技能自带的专用工具，加载后才出现在工具列表里
- 参考图只在对照判断时才发给看图模型
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

from vva.agent import prompts
from vva.core.plugin import load_class
from vva.perception.timeline import WatchRules
from vva.skills.model import Skill, SkillObject
from vva.skills.plugin import SkillContext
from vva.skills.store import SkillStore

log = logging.getLogger(__name__)

LOADED_KEY = "loaded_skill"      # load_skill 的结果里带这个字段，用来判断某个会话加载过哪些技能


class SkillManager:
    def __init__(self, cfg: dict[str, Any], camera_id: str = "cam0") -> None:
        root = cfg.get("dir") or "skills"
        self.store = SkillStore(root)
        legacy = cfg.get("legacy_scene_dir") or ""
        if legacy and os.path.isdir(legacy):
            self.store.migrate_scene(legacy)
        self.camera_id = camera_id
        self.max_ref_images = max(1, int(cfg.get("max_ref_images", 3)))
        self.max_objects = max(1, int(cfg.get("max_objects_per_call", 2)))
        self.always_max_chars = int(cfg.get("always_max_chars", 800))
        self._listeners: list[Callable[[], None]] = []
        self.plugins: dict[str, Any] = {}          # 技能名 → 专用工具（ToolProvider）
        self._tool_owner: dict[str, str] = {}      # 工具名 → 所属技能

    # ---------------------------------------------------------------- 状态
    def active(self) -> list[Skill]:
        return [s for s in self.store.skills.values() if s.enabled and s.applies_to(self.camera_id)]

    def get_active(self, name: str) -> Skill | None:
        name = (name or "").strip()
        return next((s for s in self.active() if s.name == name), None)

    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def changed(self) -> None:
        """网页修改技能后调用：重新读取，并通知（更新热词、规则等）。"""
        self.store.reload()
        for fn in self._listeners:
            fn()

    # ---------------------------------------------------------------- 专用工具
    def load_plugins(self, buffer: Any, vlm: Any, bus: Any) -> None:
        """加载各技能目录下 tools.py 里的 Tools 类。代码只在启动时加载，修改后需重启服务。"""
        self.plugins.clear()
        self._tool_owner.clear()
        for s in self.store.skills.values():
            if not s.has_tools:
                continue
            path = os.path.join(self.store.root, s.dirname, "tools.py")
            try:
                ctx = SkillContext(s.name, os.path.dirname(path), buffer, vlm, bus)
                provider = load_class(f"{path}:Tools")(ctx)
                names = provider.names()
            except Exception as e:
                log.error("技能“%s”的 tools.py 加载失败：%s", s.name, e)
                continue
            clash = names & set(self._tool_owner)
            if clash:
                log.error("技能“%s”的工具名与其他技能重复：%s，已跳过", s.name, "、".join(clash))
                continue
            self.plugins[s.name] = provider
            for n in names:
                self._tool_owner[n] = s.name
            log.info("已加载技能“%s”的专用工具：%s", s.name, "、".join(sorted(names)))

    def plugin_owner(self, tool_name: str) -> str | None:
        return self._tool_owner.get(tool_name)

    @staticmethod
    def loaded_in(messages: list[dict[str, Any]]) -> set[str]:
        """从会话消息里找出已经用 load_skill 加载过的技能。"""
        out: set[str] = set()
        for m in messages:
            if m.get("role") == "tool" and LOADED_KEY in (m.get("content") or ""):
                try:
                    name = json.loads(m["content"]).get(LOADED_KEY)
                except (ValueError, AttributeError):
                    continue
                if name:
                    out.add(name)
        return out

    def tool_visible(self, messages: list[dict[str, Any]], tool_name: str) -> bool:
        """技能专用工具：所属技能已启用且在本会话中加载过才可见；其他工具不受影响。"""
        owner = self._tool_owner.get(tool_name)
        if owner is None:
            return True
        skill = self.get_active(owner)
        return skill is not None and (skill.always or owner in self.loaded_in(messages))

    # ---------------------------------------------------------------- 提示词
    def prompt_block(self) -> str:
        """追加到智能体系统提示词：常驻技能的正文 + 其他技能的目录。"""
        active = self.active()
        if not active:
            return ""
        always = ""
        for s in (x for x in active if x.always):
            always += f"### {s.name}\n{s.body[:self.always_max_chars]}\n"
            if s.objects:
                always += "特定目标：" + "；".join(o.to_text() for o in s.objects.values()) + "\n"
            always += "\n"
        others = [s for s in active if not s.always]
        catalog = ""
        if others:
            items = []
            for s in others:
                extra = []
                if s.objects:
                    extra.append("目标：" + "、".join(s.objects))
                if s.name in self.plugins:
                    extra.append("带专用工具")
                items.append(f"- {s.name}：{s.description or '（无描述）'}" + (f"（{'；'.join(extra)}）" if extra else ""))
            catalog = prompts.SKILLS_CATALOG.format(items="\n".join(items))
        return prompts.SKILLS_BLOCK.format(always=always, catalog=catalog)

    def observer_hint(self) -> str:
        """后台观察用：常驻技能的正文（截断）。"""
        return "\n".join(s.body[:self.always_max_chars] for s in self.active() if s.always and s.body)

    def asr_prompt(self) -> str:
        words: list[str] = []
        for s in self.active():
            words += s.hotwords
            for o in s.objects.values():
                words += o.names
        return "、".join(dict.fromkeys(w for w in words if w))

    def apply_rules(self, rules: WatchRules) -> int:
        """把已启用技能里启用的规则模板加入监控提醒（相同条件不重复添加）。"""
        existing = {r.condition for r in rules.list()}
        n = 0
        for s in self.active():
            for t in s.rules:
                if t.enabled and t.condition not in existing:
                    rules.add(t.condition, t.cooldown_s)
                    existing.add(t.condition)
                    n += 1
        if n:
            log.info("从技能加载 %d 条监控规则", n)
        return n

    # ---------------------------------------------------------------- 目标与参考图
    def objects(self) -> list[tuple[Skill, SkillObject]]:
        return [(s, o) for s in self.active() for o in s.objects.values()]

    def find_object(self, name: str) -> tuple[Skill, SkillObject] | None:
        name = (name or "").strip()
        for s, o in self.objects():
            if name in o.names:
                return s, o
        hits = self.match(name)
        return hits[0] if hits else None

    def match(self, text: str) -> list[tuple[Skill, SkillObject]]:
        """找出文字里提到的目标（按出现先后），最多 max_objects 个。"""
        hits: list[tuple[int, Skill, SkillObject]] = []
        for s, o in self.objects():
            pos = [text.find(n) for n in o.names if n and n in text]
            if pos:
                hits.append((min(pos), s, o))
        return [(s, o) for _, s, o in sorted(hits, key=lambda x: x[0])][:self.max_objects]

    def references(self, skill: Skill, obj: SkillObject) -> list[bytes]:
        imgs = [self.store.read_image(skill.name, obj.name, f) for f in obj.images[:self.max_ref_images]]
        return [b for b in imgs if b]

    def references_for_text(self, text: str) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        for s, o in self.match(text):
            for i, img in enumerate(self.references(s, o), 1):
                out.append((f"“{o.name}”的参考图{i}", img))
        return out
