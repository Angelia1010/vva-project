"""职责：岗位上的 AI 值守员要负责的事，以及“发现 → 复核 → 告警 → 通知”的执行。

对外只需要 DutyManager 这一个入口：
    duties = DutyManager(cfg["duties"], db, bus, buffer, vlm, llm, report=..., scene=..., references=...)
    observer.on_hit = duties.engine.on_hit         # 后台观察的疑似命中交给职责引擎
    tools = ToolSet(..., duties.tools)             # 智能体的职责工具
    agent.extra_prompt → duties.prompt_block()     # “我负责什么”写进系统提示词
    tasks = duties.background_tasks()              # 巡检、汇报定时执行

  model.py      职责数据结构        store.py     存取与职责清单（兼容旧监控规则接口）
  engine.py     命中 → 告警的规则    verify.py    放大复核
  scheduler.py  巡检、汇报定时执行   parse.py     一句话 → 表单
  templates.py  行业模板            tools.py     智能体工具
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Coroutine

from vva.alerts import AlertService, AlertStore
from vva.core.bus import TOPIC_UI, EventBus
from vva.core.sqlite import SqliteDB
from vva.duties.engine import DutyEngine
from vva.duties.model import Duty
from vva.duties.parse import parse_duty
from vva.duties.scheduler import DutyScheduler
from vva.duties.store import DutyBook, DutyStore
from vva.duties.tools import DutyTools
from vva.duties.verify import Verifier
from vva.notify import NotifyRouter, describe_target

log = logging.getLogger(__name__)

__all__ = ["Duty", "DutyBook", "DutyManager", "DutyStore"]


class DutyManager:
    def __init__(self, cfg: dict[str, Any], db: SqliteDB, bus: EventBus, buffer: Any, vlm: Any, llm: Any,
                 report: Callable[[float, float], Awaitable[str]], scene: Callable[[], str] = lambda: "",
                 station_name: Callable[[], str] = lambda: "",
                 references: Callable[[str], list[tuple[str, bytes]]] | None = None,
                 observe_interval: float = 8.0) -> None:
        self.cfg, self.bus, self.llm, self.scene = cfg, bus, llm, scene
        self.book = DutyBook(DutyStore(db))
        self.alerts = AlertService(AlertStore(db, cfg.get("alerts_dir") or "data/alerts"), bus)
        # 由应用设置：可选的通知对象 [{"id", "label"}]，以及某个通知对象的问题（如“钉钉未开启”），没有问题返回空串
        self.target_options: Callable[[], list[dict[str, str]]] = lambda: [
            {"id": "web", "label": "值守台"}, {"id": "voice", "label": "现场播报"}]
        self.target_problem: Callable[[str], str] = lambda t: ""
        self.router = NotifyRouter(bus, station_name, labels=lambda: {o["id"]: o["label"] for o in self.target_options()})
        self.verifier = Verifier(buffer, vlm, delay=cfg.get("verify_delay", 2.0), max_side=cfg.get("verify_side", 1280),
                                 scene=scene, references=references, negatives=self._negative_images)
        self.engine = DutyEngine(self.book, self.alerts, self.router, bus, self.verifier, observe_interval)
        self.scheduler = DutyScheduler(self.book, self.engine, self.router, bus, buffer, vlm, report, scene,
                                       max_side=cfg.get("verify_side", 1280))
        self.tools = DutyTools(self, bus)
        self.book.on_change(lambda _: bus.publish(TOPIC_UI, {"type": "duties_changed", "session": None}))

    def _negative_images(self, duty: Duty) -> list[bytes]:
        out = []
        for rel in duty.negatives[-2:]:
            path = self.alerts.store.image_path(rel)
            if path:
                with open(path, "rb") as f:
                    out.append(f.read())
        return out

    def add_negative(self, alert: Any) -> bool:
        """把一条误报的截图加入职责的“不算”例子（最多保留 3 张）。"""
        duty = self.book.get(alert.duty_id)
        if duty is None or not alert.images:
            return False
        rel = alert.images[-1]["file"]
        self.book.update(duty.id, actor="值班员", negatives=[*[n for n in duty.negatives if n != rel], rel][-3:])
        return True

    # ---------------------------------------------------------------- 展示
    def target_labels(self, targets: list[str]) -> str:
        labels = {o["id"]: o["label"] for o in self.target_options()}
        return "、".join(describe_target(t, labels) for t in targets)

    def state_of(self, d: Duty, now: float) -> tuple[str, str]:
        """(状态, 说明)：running 运行中 | idle 未到生效时间 | paused 已暂停 | trial 试运行 | error 异常"""
        problems = [p for p in (self.target_problem(t) for t in d.effective_targets()) if p]
        if not d.enabled:
            return "paused", "已暂停"
        if problems and d.mode != "trial":
            return "error", "异常：" + "；".join(dict.fromkeys(problems))
        if d.kind == "watch" and not d.in_schedule(now):
            return "idle", "未到生效时间"
        if d.mode == "trial":
            return "trial", "试运行，只记录不通知"
        if d.kind == "inspect":
            return "running", f"上次：{d.last_result}" if d.last_result else "等待到点检查"
        if d.kind == "report":
            return "running", d.last_result or "等待到点发送"
        return "running", "运行中"

    def status_list(self) -> list[dict[str, Any]]:
        now = time.time()
        today = self.alerts.duty_today()
        out = []
        for d in self.book.list():
            if d.expired(now):
                continue
            state, text = self.state_of(d, now)
            info = today.get(d.id, {})
            out.append({**d.to_dict(), "state": state, "state_text": text, "today_count": info.get("count", 0),
                        "target_text": self.target_labels(d.effective_targets())})
        return out

    def summary(self) -> str:
        """一句话概括当前职责，如“长期职责 4 项：禁止吸烟、离岗提醒、……；临时任务 1 个：快递提醒”。"""
        items = self.status_list()
        long = [d["name"] for d in items if not d["temporary"]]
        temp = [d["name"] for d in items if d["temporary"]]
        text = f"长期职责 {len(long)} 项" + (f"：{'、'.join(long)}" if long else "")
        return text + (f"；临时任务 {len(temp)} 个：{'、'.join(temp)}" if temp else "；没有临时任务")

    def prompt_block(self) -> str:
        items = self.status_list()
        if not items:
            return "\n## 你的职责（实时）\n目前没有配置任何职责。被问到时如实说明，并提示可以在值守台“添加职责”。\n"
        long = [d for d in items if not d["temporary"]]
        temp = [d for d in items if d["temporary"]]
        lines = []
        for d in long:
            lines.append(f"- {d['id']} {d['name']}（{d['kind_label']}；{d['schedule']}；通知{d['target_text']}；"
                         f"{d['state_text']}；今天 {d['today_count']} 次）")
        for d in temp:
            lines.append(f"- {d['id']} {d['name']}（临时任务，{d['expires_text']} 到期；通知{d['target_text']}；"
                         f"{d['state_text']}）")
        return (f"\n## 你的职责（实时：长期职责 {len(long)} 项，临时任务 {len(temp)} 个。"
                "职责随时会被增删，之前对话里说过的数量和名称作废，一律以这里为准）\n" + "\n".join(lines) + "\n")

    # ---------------------------------------------------------------- 管理
    async def parse(self, text: str, objects: list[str]) -> dict[str, Any]:
        return await parse_duty(self.llm, text, self.target_options(), objects, self.scene())

    async def test(self, duty: Duty) -> dict[str, Any]:
        """试一下：用当前画面检查一次，不发通知、不计入告警。"""
        if duty.kind == "inspect":
            r = await self.scheduler.inspect(duty)
            return {"ok": True, "match": bool(r.get("abnormal")), "evidence": f"{r.get('result', '')}。{r.get('evidence', '')}",
                    "image": r.get("jpeg")}
        if duty.kind == "report":
            return {"ok": True, "match": False, "evidence": "汇报类职责到点发送报告，无需检查画面"}
        r = await self.verifier.check(duty, wait=False)
        return {"ok": True, "match": r.ok, "evidence": r.evidence or ("不成立" if not r.ok else ""), "box": r.box,
                "image": r.images[0][1] if r.images else None}

    def background_tasks(self) -> list[Coroutine[Any, Any, None]]:
        return [self.scheduler.run(), self._cleanup_loop()]

    async def _cleanup_loop(self) -> None:
        """每天清理一次超过保留期的告警截图。"""
        while True:
            try:
                n = self.alerts.store.cleanup(float(self.cfg.get("alert_image_days", 90)))
                if n:
                    log.info("已清理 %d 天的告警截图", n)
            except Exception as e:
                log.warning("清理告警截图失败：%s", e)
            await asyncio.sleep(86400)

    async def close(self) -> None:
        await self.engine.close()
