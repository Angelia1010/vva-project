"""搜索子智能体：一句话 → 录像里的具体片段（哪一路、哪一秒、画面哪里）。

固定四步，每步都可以单独关掉，超出预算就带着已有结果返回：
  1. 理解问题   大模型把一句话变成检索条件（时间段、摄像头、关键词、要确认的情况）
  2. 粗筛       查检索索引（画面记录）和告警表，按摄像头把相邻记录合并成“事件”，按相关度取前几个
  3. 看图确认   取每个候选事件那一刻的画面交给视觉模型确认，顺带框出目标（排除误报）
  4. 精确定位   在命中点之前 30 秒的录像里找事情开始的那一刻

用固定流程而不是让模型自由调用工具：步骤可控、耗时可预估，换成更小的模型也能稳定工作。
只依赖接口：检索索引、告警记录、摄像头清单、取画面（录像回放 / 长期记忆关键帧）、大模型、视觉模型。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from vva.agent.prompts import COORD_RULES
from vva.core.jsonutil import parse_json
from vva.memory.summarizer import complete_text
from vva.memory.timeutil import parse_time
from vva.perception.imaging import box_to_normalized, decode_jpeg, validate_region
from vva.perception.scheduler import Superseded
from vva.search import prompts
from vva.search.index import Query, SearchIndex, grams

log = logging.getLogger(__name__)
_WEEK = "一二三四五六日"


@dataclass
class Event:
    camera: str
    start: float
    end: float
    texts: list[str] = field(default_factory=list)
    score: float = 0.0
    alert_id: int | None = None
    alert_evidence: str = ""
    alert_box: list[float] | None = None
    alert_image: str = ""
    # 看图确认后填写
    ts: float = 0.0
    evidence: str = ""
    confidence: str = ""
    box: list[float] | None = None
    jpeg: bytes | None = None
    verified: bool = False


@dataclass
class Plan:
    start: float
    end: float
    cameras: list[str]
    keywords: list[str]
    people_min: int | None
    look_for: str


class SearchAgent:
    def __init__(self, index: SearchIndex, alerts: Any, cameras: Any, llm: Any, vlm: Any,
                 frame: Callable[[str, float], Awaitable[tuple[bytes | None, float]]],
                 scene: Callable[[str], str] | None = None, cfg: dict[str, Any] | None = None) -> None:
        """frame(摄像头, 时间) → (JPEG, 实际时间)：先取录像，没有就取长期记忆的关键帧。"""
        self.index, self.alerts, self.cameras, self.llm, self.vlm = index, alerts, cameras, llm, vlm
        self.frame, self.scene = frame, scene
        c = cfg or {}
        self.verify_frames = bool(c.get("verify_frames", True))
        self.refine_time = bool(c.get("refine_time", True))
        b = c.get("budget") or {}
        self.max_candidates = int(b.get("max_candidates", 10))
        self.max_frames = int(b.get("max_frames", 16))
        self.max_hits = int(b.get("max_hits", 8))
        self.timeout_s = float(b.get("timeout_s", 40))
        self.merge_gap_s = float(c.get("merge_gap_s", 90))

    # ---------------------------------------------------------------- 1. 理解问题
    def _camera_lines(self) -> str:
        lines = []
        for cam in self.cameras.list():
            info = self.cameras.info(cam.id)
            lines.append(f"- {cam.id}：{info.get('label') or cam.name}" + (f"（{cam.area}）" if cam.area else ""))
        return "\n".join(lines) or "- cam0：主画面"

    async def plan(self, question: str, camera: str = "", start: str = "", end: str = "",
                   focus: str | None = None, now: float | None = None) -> Plan:
        now = now or time.time()
        t = time.localtime(now)
        scope = ""
        if focus and self.cameras.get(focus):
            scope = f"值班员此刻在看 {focus}（{self.cameras.label(focus)}）；说“这里”“这边”时指这一路，没说哪一路时搜全部摄像头。\n"
        text = await complete_text(self.llm, [
            {"role": "system", "content": prompts.PLAN_SYSTEM},
            {"role": "user", "content": prompts.PLAN_PROMPT.format(
                now=time.strftime("%Y-%m-%d %H:%M", t), weekday="星期" + _WEEK[t.tm_wday], cameras=self._camera_lines(),
                default_scope=scope, question=question)}])
        data = parse_json(text)
        data = data if isinstance(data, dict) else {}

        def ts(v: Any, default: float) -> float:
            try:
                return parse_time(str(v), now) if v else default
            except ValueError:
                return default
        p_start = ts(start or data.get("start"), now - 86400)
        p_end = min(now, ts(end or data.get("end"), now))
        if p_end <= p_start:
            p_start, p_end = now - 86400, now
        cams = []
        for c in ([camera] if camera else list(data.get("cameras") or [])):
            cam = self.cameras.resolve(c) if c else None
            if cam:
                cams.append(cam.id)
        kws = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()][:10]
        if not kws:                                     # 模型没给关键词：用问题里的词
            kws = [w for w in grams(question).split() if len(w) >= 2][:6]
        pm = data.get("people_min")
        return Plan(p_start, p_end, list(dict.fromkeys(cams)), kws, int(pm) if isinstance(pm, (int, float)) else None,
                    str(data.get("look_for") or question).strip())

    # ---------------------------------------------------------------- 2. 粗筛
    def candidates(self, plan: Plan) -> list[Event]:
        entries = self.index.query(Query(plan.start, plan.end, plan.cameras, plan.keywords, plan.people_min))
        events: list[Event] = []
        by_cam: dict[str, list[Any]] = {}
        for e in entries:
            by_cam.setdefault(e.camera, []).append(e)
        for cam, items in by_cam.items():
            cur: Event | None = None
            for e in items:
                score = len(e.matched) or 1
                if cur and e.ts - cur.end <= self.merge_gap_s:
                    cur.end = e.ts
                    cur.score += score
                    if len(cur.texts) < 3:
                        cur.texts.append(e.text)
                else:
                    cur = Event(cam, e.ts, e.ts, [e.text], float(score))
                    events.append(cur)
        default_cam = self.cameras.default_id()
        kws = [k for k in plan.keywords if k]
        for a in self.alerts.query(start=plan.start, end=plan.end, limit=300):
            if a.status in ("record",) and a.kind != "inspect":
                continue
            cam = a.camera or default_cam
            if plan.cameras and cam not in plan.cameras:
                continue
            hay = f"{a.duty_name}{a.condition}{a.evidence}"
            if kws and not any(k in hay for k in kws):
                continue
            near = next((ev for ev in events if ev.camera == cam and ev.start - self.merge_gap_s <= a.ts <= ev.end + self.merge_gap_s), None)
            ev = near or Event(cam, a.ts, a.ts, [], 0.0)
            if near is None:
                events.append(ev)
            ev.score += 5 if a.status != "false" else -3          # 已确认的告警最可信，误报降权
            ev.alert_id, ev.alert_evidence, ev.alert_box = a.id, a.evidence, a.box
            ev.alert_image = a.images[-1]["file"] if a.images else ""
            ev.start, ev.end = min(ev.start, a.ts), max(ev.end, a.ts)
            ev.ts = a.ts
        events.sort(key=lambda ev: (-ev.score, -ev.start))
        return events[: self.max_candidates]

    # ---------------------------------------------------------------- 3. 看图确认
    async def verify(self, ev: Event, look_for: str) -> bool | None:
        """True 确认、False 否定、None 没有画面可看。"""
        t = ev.ts or (ev.start + ev.end) / 2
        jpeg, real = await self.frame(ev.camera, t)
        if not jpeg:
            return None
        ev.jpeg, ev.ts = jpeg, real
        img = decode_jpeg(jpeg)
        h, w = img.shape[:2]
        fmt = getattr(self.vlm, "bbox_format", "0-1000")
        scene = self.scene(ev.camera) if self.scene else ""
        hint = f"画面记录：{'；'.join(ev.texts[:2])}\n" if ev.texts else ""
        prompt = prompts.VERIFY_PROMPT.format(scene=f"场景：{scene}\n" if scene else "", look_for=look_for, hint=hint,
                                              when=time.strftime("%m-%d %H:%M:%S", time.localtime(real)),
                                              coord_rule="坐标" + COORD_RULES.get(fmt, COORD_RULES["0-1000"]) + "。")
        data = parse_json(await self.vlm.ask([jpeg], prompt, json_mode=True))
        if not isinstance(data, dict):
            return None
        ok = bool(data.get("match")) and data.get("confidence") != "低"
        ev.evidence = str(data.get("evidence") or "")
        ev.confidence = str(data.get("confidence") or "")
        if data.get("box_2d"):
            ev.box = validate_region(box_to_normalized(data["box_2d"], fmt, w, h))
        ev.verified = ok
        return ok

    # ---------------------------------------------------------------- 4. 精确定位
    async def refine(self, ev: Event, look_for: str) -> int:
        """在命中点之前 30 秒内找开始时刻。返回用掉的画面数。"""
        times = [ev.ts - 30, ev.ts - 20, ev.ts - 10, ev.ts - 5]
        frames: list[tuple[bytes, float]] = []
        for t in times:
            jpeg, real = await self.frame(ev.camera, t)
            if jpeg and abs(real - t) < 4:              # 只用录像里真实存在的那一秒
                frames.append((jpeg, real))
        if len(frames) < 2:
            return 0
        frames.append((ev.jpeg, ev.ts))
        prompt = prompts.REFINE_PROMPT.format(n=len(frames), look_for=look_for,
                                              times="、".join(time.strftime("%H:%M:%S", time.localtime(t)) for _, t in frames))
        data = parse_json(await self.vlm.ask([j for j, _ in frames], prompt, json_mode=True))
        k = data.get("first") if isinstance(data, dict) else None
        if isinstance(k, (int, float)) and 1 <= int(k) <= len(frames):
            ev.start = min(ev.start, frames[int(k) - 1][1])
        return len(frames)

    # ---------------------------------------------------------------- 全流程
    async def run(self, question: str, *, camera: str = "", start: str = "", end: str = "", focus: str | None = None,
                  progress: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
        deadline = time.time() + self.timeout_s
        notes: list[str] = []
        progress("正在理解问题…")
        plan = await self.plan(question, camera, start, end, focus)
        scope = self.scope_text(plan)
        progress(f"检索 {scope}：{'、'.join(plan.keywords[:4])}")
        events = await asyncio.to_thread(self.candidates, plan)
        hits: list[Event] = []
        frames_used = 0
        if not events:
            progress("没有找到相关记录")
        elif not self.verify_frames:
            hits = events[: self.max_hits]
        else:
            progress(f"找到 {len(events)} 个候选，正在逐个看画面确认…")
            for i, ev in enumerate(events):
                if time.time() > deadline or frames_used >= self.max_frames:
                    notes.append(f"时间或画面数到了上限，只确认了前 {i} 个候选")
                    break
                progress(f"正在确认第 {i + 1}/{len(events)} 个候选（{self.cameras.label(ev.camera) or ev.camera} "
                         f"{time.strftime('%H:%M', time.localtime(ev.ts or ev.start))}）")
                try:
                    ok = await self.verify(ev, plan.look_for)
                except Superseded:
                    ok = None
                frames_used += 1
                if ok is None and (ev.alert_id or ev.score >= 2):   # 没有画面可看：告警或多次命中的记录照样算，标为未确认
                    ev.evidence = ev.alert_evidence or (ev.texts[0] if ev.texts else "")
                    ev.confidence = "未看图确认"
                    ev.box = ev.box or ev.alert_box
                    hits.append(ev)
                elif ok:
                    hits.append(ev)
                if len(hits) >= self.max_hits:
                    break
            if self.refine_time:
                for ev in hits:
                    if not ev.verified or time.time() > deadline or frames_used >= self.max_frames:
                        continue
                    progress(f"正在精确定位 {self.cameras.label(ev.camera) or ev.camera} "
                             f"{time.strftime('%H:%M', time.localtime(ev.ts))} 的开始时刻…")
                    try:
                        frames_used += await self.refine(ev, plan.look_for)
                    except Superseded:
                        pass
        hits.sort(key=lambda e: e.start)
        return {"plan": plan, "scope": scope, "hits": hits, "notes": notes, "candidates": len(events)}

    def scope_text(self, plan: Plan) -> str:
        fmt = "%m-%d %H:%M"
        a, b = time.localtime(plan.start), time.localtime(plan.end)
        when = time.strftime(fmt, a) + "～" + (time.strftime("%H:%M", b) if a[:3] == b[:3] else time.strftime(fmt, b))
        where = "、".join(self.cameras.label(c) or c for c in plan.cameras) if plan.cameras else "所有摄像头"
        return f"{when} · {where}"
