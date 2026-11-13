"""智能体可调用的视觉工具。每个工具返回 JSON 字符串给大模型。

新增工具：在 schemas() 中加一个定义，并实现同名的 async 方法即可。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from vva.agent import prompts
from vva.core.bus import TOPIC_UI, EventBus
from vva.core.jsonutil import parse_json, strip_think
from vva.core.types import fmt_ts
from vva.perception.imaging import (box_to_normalized, crop_region, decode_jpeg, encode_jpeg,
                                    resize_max_side, validate_region)
from vva.perception.timeline import Timeline, WatchRules
from vva.video.frame_buffer import BufferedFrame, FrameBuffer

log = logging.getLogger(__name__)

_REGION_SCHEMA = {
    "type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
    "description": "可选。只看画面中的某个区域并放大，格式 [x1,y1,x2,y2]，0~1 的相对坐标，左上角为原点。"
                   "通常先用 locate 得到。",
}


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


class VisionTools:
    def __init__(self, buffer: FrameBuffer, vlm: Any, timeline: Timeline, rules: WatchRules,
                 bus: EventBus, look_max_side: int = 1280, crop_min_side: int = 768, **_: Any) -> None:
        self.buffer, self.vlm, self.timeline, self.rules, self.bus = buffer, vlm, timeline, rules, bus
        self.look_max_side = int(look_max_side)
        self.crop_min_side = int(crop_min_side)

    # ------------------------------------------------------------------ schema
    def schemas(self) -> list[dict]:
        q = {"type": "string", "description": "要在画面中弄清楚的具体问题"}
        return [
            _fn("look_now", "看摄像头的当前画面并回答问题。", {"question": q, "region": _REGION_SCHEMA}, ["question"]),
            _fn("locate", "在当前画面中定位目标，返回目标的框坐标（0~1），可用于 look_now 的 region。",
                {"target": {"type": "string", "description": "要找的目标，如“墙上的压力表”"}}, ["target"]),
            _fn("look_back", "看过去某个时刻的画面（最多回看帧缓存的时长）。",
                {"seconds_ago": {"type": "number", "description": "多少秒之前"}, "question": q,
                 "region": _REGION_SCHEMA}, ["seconds_ago", "question"]),
            _fn("look_sequence", "看最近一段时间内按时间顺序排列的多帧画面，用于判断动作、变化或先后顺序。",
                {"seconds": {"type": "number", "description": "最近多少秒"},
                 "question": q,
                 "frames": {"type": "integer", "description": "抽取的帧数，2~8，默认4"}},
                ["seconds", "question"]),
            _fn("search_timeline", "查询后台记录的画面时间线（文字摘要）。",
                {"minutes": {"type": "number", "description": "查询最近多少分钟，默认10"}}, []),
            _fn("add_watch", "添加一条监控提醒规则，条件满足时系统会主动提醒用户。",
                {"condition": {"type": "string", "description": "可从单张画面判断的条件，如“画面中没有人坐在电脑前”"},
                 "cooldown_seconds": {"type": "number", "description": "两次提醒之间的最短间隔，默认60"}},
                ["condition"]),
            _fn("list_watches", "列出当前所有监控提醒规则。", {}, []),
            _fn("remove_watch", "删除一条监控提醒规则。",
                {"rule_id": {"type": "string", "description": "规则编号，如 r1；传 all 删除全部"}}, ["rule_id"]),
        ]

    # ------------------------------------------------------------------ dispatch
    async def call(self, name: str, args: dict[str, Any]) -> str:
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False)
        self.bus.publish(TOPIC_UI, {"type": "tool", "name": name, "args": args})
        t0 = time.time()
        try:
            result = await fn(**args)
        except TypeError as e:
            result = {"error": f"参数错误：{e}"}
        except Exception as e:
            log.exception("工具 %s 执行失败", name)
            result = {"error": f"执行失败：{e}"}
        log.info("工具 %s 耗时 %.2fs", name, time.time() - t0)
        return json.dumps(result, ensure_ascii=False)

    # ------------------------------------------------------------------ helpers
    def _prepare(self, bf: BufferedFrame, region: list[float] | None) -> bytes:
        img = decode_jpeg(bf.jpeg)
        if region:
            img = crop_region(img, region, min_side=self.crop_min_side)
        return encode_jpeg(resize_max_side(img, self.look_max_side), 90)

    async def _ask(self, images: list[bytes], question: str) -> str:
        return strip_think(await self.vlm.ask(images, question, system=prompts.LOOK_SYSTEM))

    # ------------------------------------------------------------------ tools
    async def tool_look_now(self, question: str, region: list[float] | None = None) -> dict:
        bf = self.buffer.latest_snapshot(quality=92)
        if bf is None:
            return {"error": "暂时没有画面，请检查摄像头"}
        reg = validate_region(region)
        if reg:
            self.bus.publish(TOPIC_UI, {"type": "bbox", "boxes": [{"box": reg, "label": "放大区域"}]})
        answer = await self._ask([self._prepare(bf, reg)], question)
        return {"time": fmt_ts(bf.ts), "region": reg, "answer": answer}

    async def tool_locate(self, target: str) -> dict:
        bf = self.buffer.latest_snapshot(max_side=self.look_max_side, quality=90)
        if bf is None:
            return {"error": "暂时没有画面，请检查摄像头"}
        fmt = getattr(self.vlm, "bbox_format", "0-1000")
        prompt = prompts.LOCATE_PROMPT.format(target=target, coord_rule=prompts.COORD_RULES.get(fmt, prompts.COORD_RULES["0-1000"]))
        data = parse_json(await self.vlm.ask([bf.jpeg], prompt, system=prompts.LOOK_SYSTEM, json_mode=True))
        objs = []
        items = data.get("objects", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for o in items:
            if not isinstance(o, dict):
                continue
            box = box_to_normalized(o.get("box_2d") or o.get("bbox") or o.get("box"), fmt, bf.width, bf.height)
            if box:
                objs.append({"label": str(o.get("label", target)), "box": [round(v, 3) for v in box]})
        if objs:
            self.bus.publish(TOPIC_UI, {"type": "bbox", "boxes": objs})
        return {"time": fmt_ts(bf.ts), "found": bool(objs), "objects": objs}

    async def tool_look_back(self, seconds_ago: float, question: str, region: list[float] | None = None) -> dict:
        bf = self.buffer.at(float(seconds_ago))
        if bf is None:
            return {"error": "帧缓存中没有那个时间的画面"}
        reg = validate_region(region)
        answer = await self._ask([self._prepare(bf, reg)], question)
        return {"time": fmt_ts(bf.ts), "seconds_ago": round(time.time() - bf.ts, 1), "answer": answer}

    async def tool_look_sequence(self, seconds: float, question: str, frames: int = 4) -> dict:
        frames = min(8, max(2, int(frames or 4)))
        bfs = self.buffer.range(float(seconds), frames)
        if not bfs:
            return {"error": "帧缓存中没有这段时间的画面"}
        imgs = [self._prepare(b, None) for b in bfs]
        times = "、".join(fmt_ts(b.ts) for b in bfs)
        q = f"以下 {len(imgs)} 张图片按时间先后排列，拍摄时间依次为：{times}。\n问题：{question}"
        answer = await self._ask(imgs, q)
        return {"from": fmt_ts(bfs[0].ts), "to": fmt_ts(bfs[-1].ts), "frames": len(imgs), "answer": answer}

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
