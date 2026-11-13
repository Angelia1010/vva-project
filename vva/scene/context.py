"""把场景包变成模型能用的上下文：提示词、按问题匹配目标、取参考图、语音热词。"""
from __future__ import annotations

from vva.scene.store import SceneObject, SceneStore


class SceneContext:
    def __init__(self, store: SceneStore, max_ref_images: int = 3, max_objects_per_call: int = 2) -> None:
        self.store = store
        self.max_ref_images = max(1, int(max_ref_images))
        self.max_objects = max(1, int(max_objects_per_call))

    @property
    def scene(self):
        return self.store.scene

    # ---------------------------------------------------------------- 文字
    def prompt_block(self) -> str:
        """追加到智能体系统提示词的场景说明。场景包为空时返回空字符串。"""
        s = self.scene
        lines: list[str] = []
        if s.description:
            lines.append(f"场景概况：{s.description}")
        if s.areas:
            lines.append("画面区域：" + "；".join(f"{k}是{v}" for k, v in s.areas.items()))
        if s.standards:
            lines.append("判断标准：\n" + "\n".join(f"- {x}" for x in s.standards))
        if s.objects:
            lines.append("现场的特定目标（问到它们、或需要判断它们的状态时，用 compare_with_reference 对照参考图）：\n"
                         + "\n".join(f"- {o.to_text()}" for o in s.objects.values()))
        return "\n\n## 场景包：" + s.name + "\n" + "\n".join(lines) if lines else ""

    def observer_hint(self) -> str:
        """给后台观察用的简短场景说明（不含参考图提示）。"""
        s = self.scene
        parts = [s.description] if s.description else []
        if s.areas:
            parts.append("区域：" + "；".join(f"{k}是{v}" for k, v in s.areas.items()))
        if s.standards:
            parts.append("判断标准：" + "；".join(s.standards))
        return "\n".join(parts)

    def asr_prompt(self) -> str:
        words = list(dict.fromkeys([*self.scene.hotwords, *(n for o in self.scene.objects.values() for n in o.names)]))
        return "、".join(words)

    # ---------------------------------------------------------------- 目标与参考图
    def find(self, name: str) -> SceneObject | None:
        name = (name or "").strip()
        for o in self.scene.objects.values():
            if name in o.names:
                return o
        hits = self.match(name)
        return hits[0] if hits else None

    def match(self, text: str) -> list[SceneObject]:
        """找出文字里提到的目标（按名称、别名出现的先后），最多 max_objects 个。"""
        hits: list[tuple[int, SceneObject]] = []
        for o in self.scene.objects.values():
            pos = [text.find(n) for n in o.names if n and n in text]
            if pos:
                hits.append((min(pos), o))
        return [o for _, o in sorted(hits, key=lambda x: x[0])][:self.max_objects]

    def references(self, obj: SceneObject) -> list[bytes]:
        imgs = [self.store.read_image(obj.name, f) for f in obj.images[:self.max_ref_images]]
        return [b for b in imgs if b]

    def references_for_text(self, text: str) -> list[tuple[str, bytes]]:
        """文字里提到的目标的参考图：[(说明, jpeg)]。"""
        out: list[tuple[str, bytes]] = []
        for o in self.match(text):
            for i, img in enumerate(self.references(o), 1):
                out.append((f"“{o.name}”的参考图{i}", img))
        return out
