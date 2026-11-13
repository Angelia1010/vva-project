"""智能体可调用的视觉工具：看当前/过去画面、定位、查短期时间线、监控提醒。

新增工具：在 schemas() 中加一个定义，并实现同名的 `tool_<name>` 异步方法即可。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from vva.agent import prompts
from vva.agent.toolkit import ToolProvider, fn_schema as _fn
from vva.core.bus import TOPIC_UI, EventBus
from vva.core.jsonutil import parse_json
from vva.core.types import fmt_ts
from vva.perception.look import ask_look
from vva.perception.imaging import (box_to_normalized, crop_region, decode_jpeg, encode_jpeg,
                                    resize_max_side, validate_region)
from vva.perception.timeline import Timeline, WatchRules
from vva.video.frame_buffer import BufferedFrame, FrameBuffer

log = logging.getLogger(__name__)

# 放大看局部时，智能体只说“看什么”，由程序先定位再裁剪，不让模型自己填坐标（小模型容易编造坐标而裁错区域）
_TARGET_SCHEMA = {
    "type": "string",
    "description": "可选。需要放大看清的目标，如“表盘”“桌上的水杯”“屏幕上的文字”。"
                   "填写后会先自动定位该目标，再裁剪放大回答；找不到时自动改看整幅画面。",
}


class VisionTools(ToolProvider):
    def __init__(self, buffer: FrameBuffer, vlm: Any, timeline: Timeline, rules: WatchRules,
                 bus: EventBus, look_max_side: int = 1280, crop_min_side: int = 768,
                 look_max_tokens: int = 256, scene_hint: str = "", **_: Any) -> None:
        super().__init__(bus)
        self.buffer, self.vlm, self.timeline, self.rules = buffer, vlm, timeline, rules
        self.look_max_side = int(look_max_side)
        self.crop_min_side = int(crop_min_side)
        self.look_max_tokens = int(look_max_tokens)
        self.scene_hint = scene_hint

    # ------------------------------------------------------------------ schema
    def schemas(self) -> list[dict]:
        q = {"type": "string", "description": "要在画面中弄清楚的具体问题"}
        return [
            _fn("look_now", "看摄像头的当前画面并回答问题。", {"question": q, "target": _TARGET_SCHEMA}, ["question"]),
            _fn("locate", "在当前画面中找出目标的位置（会在网页画面上画框），用于回答“在哪里”。",
                {"target": {"type": "string", "description": "要找的目标，如“墙上的压力表”"}}, ["target"]),
            _fn("look_back", "看过去某个时刻的画面（最多回看帧缓存的时长）。",
                {"seconds_ago": {"type": "number", "description": "多少秒之前"}, "question": q,
                 "target": _TARGET_SCHEMA}, ["seconds_ago", "question"]),
            _fn("look_sequence", "看最近一段时间内按时间顺序排列的多帧画面，用于判断动作、变化或先后顺序。",
                {"seconds": {"type": "number", "description": "最近多少秒"},
                 "question": q,
                 "frames": {"type": "integer", "description": "抽取的帧数，2~8，默认4"}},
                ["seconds", "question"]),
            _fn("search_timeline", "查询最近一段时间（最多两小时）后台记录的画面时间线（文字摘要）。",
                {"minutes": {"type": "number", "description": "查询最近多少分钟，默认10"}}, []),
            _fn("add_watch", "添加一条监控提醒规则，条件满足时系统会主动提醒操作员。",
                {"condition": {"type": "string", "description": "可从单张画面判断的条件，如“画面中没有人坐在电脑前”"},
                 "cooldown_seconds": {"type": "number", "description": "两次提醒之间的最短间隔，默认60"}},
                ["condition"]),
            _fn("list_watches", "列出当前所有监控提醒规则。", {}, []),
            _fn("remove_watch", "删除一条监控提醒规则。",
                {"rule_id": {"type": "string", "description": "规则编号，如 r1；传 all 删除全部"}}, ["rule_id"]),
        ]

    # ------------------------------------------------------------------ helpers
    def _prepare(self, bf: BufferedFrame, region: list[float] | None) -> bytes:
        img = decode_jpeg(bf.jpeg)
        if region:
            img = crop_region(img, region, min_side=self.crop_min_side)
        return encode_jpeg(resize_max_side(img, self.look_max_side), 90)

    async def _ask(self, images: list[bytes], question: str) -> dict[str, str]:
        return await ask_look(self.vlm, images, question, self.scene_hint, self.look_max_tokens)

    async def _locate(self, bf: BufferedFrame, target: str) -> list[dict[str, Any]]:
        """在这一帧里定位目标，返回 [{"label", "box"(0~1)}]。"""
        img = resize_max_side(decode_jpeg(bf.jpeg), self.look_max_side)
        h, w = img.shape[:2]
        fmt = getattr(self.vlm, "bbox_format", "0-1000")
        prompt = prompts.LOCATE_PROMPT.format(target=target,
                                              coord_rule=prompts.COORD_RULES.get(fmt, prompts.COORD_RULES["0-1000"]))
        data = parse_json(await self.vlm.ask([encode_jpeg(img, 90)], prompt, system=prompts.LOCATE_SYSTEM,
                                             json_mode=True))
        items = data.get("objects", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        objs = []
        for o in items:
            if not isinstance(o, dict):
                continue
            box = box_to_normalized(o.get("box_2d") or o.get("bbox") or o.get("box"), fmt, w, h)
            if box:
                objs.append({"label": str(o.get("label", target)), "box": [round(v, 3) for v in box]})
        return objs

    async def _look(self, bf: BufferedFrame, question: str, target: str | None) -> dict[str, Any]:
        """看一帧画面；指定 target 时先定位再放大，定位失败就看整幅画面。"""
        info: dict[str, Any] = {"time": fmt_ts(bf.ts)}
        region = None
        if target:
            objs = await self._locate(bf, target)
            if objs:
                region = validate_region(objs[0]["box"])
                self.bus.publish(TOPIC_UI, {"type": "bbox", "boxes": [{"box": region, "label": objs[0]["label"]}]})
                info["zoomed"] = f"已定位并放大：{objs[0]['label']}"
            else:
                info["zoomed"] = f"没有定位到“{target}”，已改看整幅画面"
        return {**info, **await self._ask([self._prepare(bf, region)], question)}

    # ------------------------------------------------------------------ tools
    async def tool_look_now(self, question: str, target: str | None = None, **_: Any) -> dict:
        bf = self.buffer.latest_snapshot(quality=92)
        if bf is None:
            return {"error": "暂时没有画面，请检查摄像头"}
        return await self._look(bf, question, target)

    async def tool_locate(self, target: str) -> dict:
        bf = self.buffer.latest_snapshot(quality=92)
        if bf is None:
            return {"error": "暂时没有画面，请检查摄像头"}
        objs = await self._locate(bf, target)
        if objs:
            self.bus.publish(TOPIC_UI, {"type": "bbox", "boxes": objs})
        return {"time": fmt_ts(bf.ts), "found": bool(objs), "objects": objs}

    async def tool_look_back(self, seconds_ago: float, question: str, target: str | None = None, **_: Any) -> dict:
        bf = self.buffer.at(float(seconds_ago))
        if bf is None:
            return {"error": "帧缓存中没有那个时间的画面"}
        return {"seconds_ago": round(time.time() - bf.ts, 1), **await self._look(bf, question, target)}

    async def tool_look_sequence(self, seconds: float, question: str, frames: int = 4) -> dict:
        frames = min(8, max(2, int(frames or 4)))
        bfs = self.buffer.range(float(seconds), frames)
        if not bfs:
            return {"error": "帧缓存中没有这段时间的画面"}
        imgs = [self._prepare(b, None) for b in bfs]
        times = "、".join(fmt_ts(b.ts) for b in bfs)
        q = f"以下 {len(imgs)} 张图片按时间先后排列，拍摄时间依次为：{times}。\n问题：{question}"
        result = await self._ask(imgs, q)
        return {"from": fmt_ts(bfs[0].ts), "to": fmt_ts(bfs[-1].ts), "frames": len(imgs), **result}

    async def tool_search_timeline(self, minutes: float = 10) -> dict:
        entries = self.timeline.query(float(minutes or 10))
        if not entries:
            return {"minutes": minutes, "entries": [], "note": "这段时间没有记录（可能后台观察未开启或画面无变化）"}
        if len(entries) > 60:  # 太多时均匀抽样，控制上下文长度
            step = len(entries) / 60
            entries = [entries[int(i * step)] for i in range(60)]
        return {"minutes": minutes, "entries": [e.to_text() for e in entries]}

    async def tool_add_watch(self, condition: str, cooldown_seconds: float = 60) -> dict:
        rule = self.rules.add(condition, cooldown_seconds or 60)
        self.bus.publish(TOPIC_UI, {"type": "rules", "rules": [r.to_dict() for r in self.rules.list()]})
        return {"ok": True, "rule": rule.to_dict()}

    async def tool_list_watches(self) -> dict:
        return {"rules": [r.to_dict() for r in self.rules.list()]}

    async def tool_remove_watch(self, rule_id: str) -> dict:
        if rule_id == "all":
            for r in self.rules.list():
                self.rules.remove(r.id)
            ok = True
        else:
            ok = self.rules.remove(rule_id)
        self.bus.publish(TOPIC_UI, {"type": "rules", "rules": [r.to_dict() for r in self.rules.list()]})
        return {"ok": ok}
