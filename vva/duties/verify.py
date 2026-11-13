"""放大复核：初次命中后，过几秒再看一次——先在整幅画面上判断并定位，再把目标区域放大确认。

两次都成立才算命中，用来挡掉小目标、一闪而过、看走眼造成的误报。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from vva.agent.prompts import COORD_RULES
from vva.core.jsonutil import parse_json
from vva.duties import prompts
from vva.duties.model import Duty
from vva.perception.imaging import box_to_normalized, crop_region, decode_jpeg, encode_jpeg, resize_max_side, validate_region
from vva.video.frame_buffer import FrameBuffer

log = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    ok: bool
    evidence: str = ""
    box: list[float] | None = None
    images: list[tuple[str, bytes]] = field(default_factory=list)   # [(说明, jpeg)]


class Verifier:
    def __init__(self, buffer: FrameBuffer, vlm: Any, delay: float = 2.0, max_side: int = 1280,
                 crop_min_side: int = 768, scene: Callable[[], str] | None = None,
                 references: Callable[[str], list[tuple[str, bytes]]] | None = None) -> None:
        self.buffer, self.vlm = buffer, vlm
        self.delay, self.max_side, self.crop_min_side = float(delay), int(max_side), int(crop_min_side)
        self.scene = scene
        self.references = references

    def _scene(self) -> str:
        s = self.scene() if self.scene else ""
        return f"场景：{s}\n" if s else ""

    async def check(self, duty: Duty, evidence: str = "", wait: bool = True) -> VerifyResult:
        if wait and self.delay > 0:
            await asyncio.sleep(self.delay)
        bf = self.buffer.latest_snapshot(quality=92)
        if bf is None:
            return VerifyResult(False, "没有画面")
        img = resize_max_side(decode_jpeg(bf.jpeg), self.max_side)
        h, w = img.shape[:2]
        full = encode_jpeg(img, 90)
        refs = self.references(" ".join([duty.condition, *duty.objects])) if self.references else []
        refs = refs[:3]
        ref_text = ""
        if refs:
            ref_text = f"前 {len(refs)} 张是参考图（" + "；".join(f"图{i}是{label}" for i, (label, _) in enumerate(refs, 1)) + "），"
        fmt = getattr(self.vlm, "bbox_format", "0-1000")
        prompt = prompts.VERIFY_PROMPT.format(
            scene=self._scene(), condition=duty.condition, evidence=evidence or "无", refs=ref_text,
            image_desc="最后一张当前监控画面" if refs else "当前监控画面",
            coord_rule="坐标" + COORD_RULES.get(fmt, COORD_RULES["0-1000"]))
        data = parse_json(await self.vlm.ask([*(j for _, j in refs), full], prompt,
                                             system=prompts.VERIFY_SYSTEM, json_mode=True))
        if not isinstance(data, dict) or not data.get("match") or data.get("confidence") == "低":
            reason = data.get("evidence", "") if isinstance(data, dict) else ""
            return VerifyResult(False, reason, images=[("复核画面", full)])
        ev = str(data.get("evidence") or evidence)
        box = validate_region(box_to_normalized(data.get("box_2d"), fmt, w, h)) if data.get("box_2d") else None
        images = [("复核画面", full)]
        if box:       # 放大确认
            zoom = encode_jpeg(crop_region(img, box, min_side=self.crop_min_side), 90)
            z = parse_json(await self.vlm.ask([zoom], prompts.ZOOM_PROMPT.format(scene=self._scene(), condition=duty.condition),
                                              system=prompts.VERIFY_SYSTEM, json_mode=True))
            images.append(("放大复核", zoom))
            if not isinstance(z, dict) or not z.get("match") or z.get("confidence") == "低":
                return VerifyResult(False, z.get("evidence", "") if isinstance(z, dict) else "", box, images)
            ev = str(z.get("evidence") or ev)
        return VerifyResult(True, ev, box, images)
