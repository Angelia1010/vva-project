"""视频检索：索引（画面记录）+ 搜索子智能体 + 搜索记录。

    search = SearchService(cfg["search"], db, cameras, bus, llm, vlm, alerts_store, playback=..., memory=...)
    tools = ToolSet(..., search.tools)          # 主智能体的 search_video、control_search_result
    agent.extra_prompt → search.prompt_block()
    search.start()                              # 订阅画面记录建索引，导入长期记忆里的历史记录

可以整块关掉：
  - config.yaml 的 search.enabled: false → 不创建；
  - 网页“设置”里的开关（存在 ops.db）→ 主智能体看不到搜索工具、网页隐藏搜索框，索引照常建（keep_indexing）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any

from vva.core.bus import TOPIC_OBSERVATION, TOPIC_UI, EventBus
from vva.core.sqlite import SqliteDB
from vva.perception.imaging import decode_jpeg, encode_jpeg, resize_max_side
from vva.search.agent import Event, SearchAgent
from vva.search.index import SearchIndex, entry_text

log = logging.getLogger(__name__)

RUNS = """
CREATE TABLE IF NOT EXISTS search_runs (
  id TEXT PRIMARY KEY, ts REAL, session TEXT, question TEXT, status TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS search_settings (k TEXT PRIMARY KEY, v TEXT);
"""


class SearchService:
    def __init__(self, cfg: dict[str, Any], db: SqliteDB, cameras: Any, bus: EventBus, llm: Any, vlm: Any, alerts: Any,
                 playback: Any = None, memory: Any = None, scene: Any = None, link_base: Any = None) -> None:
        """playback：录像回放（可选）；memory：长期记忆（可选，取关键帧、导入历史）；link_base(会话) → 网页地址前缀。"""
        self.cfg, self.db, self.cameras, self.bus = cfg, db, cameras, bus
        db.script(RUNS)
        self.index = SearchIndex(db)
        self.playback, self.memory = playback, memory
        self.link_base = link_base or (lambda session: "")
        self.dir = cfg.get("dir") or "data/search"
        self.agent = SearchAgent(self.index, alerts, cameras, llm, vlm, self._frame, scene, cfg)
        self.progress: dict[str, str] = {}
        self._running: dict[str, asyncio.Task] = {}
        self.open_by_session: dict[str, str] = {}       # 会话 → 最近一次打开的搜索（语音“放大看第二个”用）
        self._tasks: list[asyncio.Task] = []
        from vva.search.tools import SearchTools
        self.tools = SearchTools(self, bus)

    # ---------------------------------------------------------------- 开关
    @property
    def enabled(self) -> bool:
        r = self.db.query("SELECT v FROM search_settings WHERE k = 'enabled'")
        return json.loads(r[0]["v"]) if r else bool(self.cfg.get("enabled", True))

    def set_enabled(self, on: bool) -> None:
        self.db.execute("INSERT OR REPLACE INTO search_settings(k, v) VALUES ('enabled', ?)", (json.dumps(bool(on)),))
        self.bus.publish(TOPIC_UI, {"type": "search_settings", "session": None, "enabled": bool(on)})

    def channel_allowed(self, session: str | None) -> bool:
        ch = "web" if not session or session == "web" or session.startswith("page:") else session.split(":")[0]
        return ch in (self.cfg.get("channels") or ["web", "dingtalk", "feishu"])

    def prompt_block(self) -> str:
        if not self.enabled:
            return ""
        return ("\n## 录像检索\n用户要找过去某段时间里发生的事、要看当时的录像（如“昨天下午有没有人抽烟”“找一下叉车经过的录像”"
                "“上次有人翻围栏是什么时候”）时用 search_video。结果会自动显示在值守台右侧的面板里，回答只用一两句话说找到几处、"
                "分别在什么时间和哪一路，不要逐条念依据。只想知道大概经过、不需要看录像时用 search_memory。"
                "结果打开后，用户说“播放第二个”“放大看看”“关掉”时用 control_search_result。\n")

    # ---------------------------------------------------------------- 取画面
    async def _frame(self, camera: str, t: float) -> tuple[bytes | None, float]:
        if self.playback is not None:
            try:
                return await asyncio.to_thread(self.playback.frame, camera, t), t
            except Exception:
                pass
        if self.memory is not None:
            ev = self.memory.store.nearest_frame(t, 120, camera) or (
                self.memory.store.nearest_frame(t, 120) if camera == self.cameras.default_id() else None)
            if ev and ev.frame:
                jpeg = self.memory.keyframes.load(ev.frame)
                if jpeg:
                    return jpeg, ev.ts
        return None, t

    # ---------------------------------------------------------------- 索引
    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._index_loop()), asyncio.create_task(self._maintain_loop())]

    async def stop(self) -> None:
        for t in [*self._tasks, *self._running.values()]:
            t.cancel()

    def import_history(self) -> int:
        """首次启动：把长期记忆里已有的画面记录导入索引，上线当天就能搜到之前的事。"""
        if self.memory is None or self.index.count():
            return 0
        days = float(self.cfg.get("retention_days", 30))
        now = time.time()
        n = 0
        for ev in self.memory.store.events_between(now - days * 86400, now, kinds=["observation"]):
            d = ev.details or {}
            self.index.add(ev.camera or self.cameras.default_id(), ev.ts,
                           entry_text(ev.summary, d.get("objects"), d.get("activities"), d.get("anomalies")),
                           ev.people, camera_name=d.get("camera_name", ""))
            n += 1
        if n:
            log.info("已把长期记忆里的 %d 条画面记录导入检索索引", n)
        return n

    async def _index_loop(self) -> None:
        await asyncio.to_thread(self.import_history)
        q = self.bus.subscribe(TOPIC_OBSERVATION)
        try:
            while True:
                msg = await q.get()
                e = msg.get("entry")
                if e is None:
                    continue
                try:
                    self.index.add(getattr(e, "camera", "") or self.cameras.default_id(), e.ts,
                                   entry_text(e.summary, e.objects, e.activities, e.anomalies), e.people_count,
                                   camera_name=getattr(e, "camera_name", ""))
                except Exception:
                    log.exception("写入检索索引失败")
        finally:
            self.bus.unsubscribe(TOPIC_OBSERVATION, q)

    async def _maintain_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                n = await asyncio.to_thread(self.index.delete_before, time.time() - float(self.cfg.get("retention_days", 30)) * 86400)
                if n:
                    log.info("检索索引清理了 %d 条过期记录", n)
            except Exception:
                log.exception("清理检索索引失败")

    # ---------------------------------------------------------------- 搜索
    @staticmethod
    def new_id() -> str:
        return time.strftime("s%Y%m%d-%H%M%S-") + f"{random.randint(0, 999):03d}"

    def _save(self, sid: str, session: str, question: str, status: str, result: dict[str, Any] | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO search_runs(id, ts, session, question, status, result) VALUES (?,?,?,?,?,?)",
                        (sid, time.time(), session, question, status, json.dumps(result, ensure_ascii=False) if result else None))

    def get(self, sid: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM search_runs WHERE id = ?", (sid,))
        if not rows:
            return None
        r = rows[0]
        return {"id": r["id"], "question": r["question"], "status": r["status"], "time": time.strftime("%m-%d %H:%M", time.localtime(r["ts"])),
                "progress": self.progress.get(sid, ""), "result": json.loads(r["result"]) if r["result"] else None}

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id, ts, question, status, result FROM search_runs WHERE status = 'done' ORDER BY ts DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            res = json.loads(r["result"]) if r["result"] else {}
            out.append({"id": r["id"], "question": r["question"], "time": time.strftime("%m-%d %H:%M", time.localtime(r["ts"])),
                        "hits": len(res.get("hits", []))})
        return out

    def thumb_path(self, sid: str, i: int) -> str:
        return os.path.join(self.dir, os.path.basename(sid), f"{int(i)}.jpg")

    def _result(self, sid: str, question: str, out: dict[str, Any]) -> dict[str, Any]:
        hits: list[Event] = out["hits"]
        items = []
        for i, ev in enumerate(hits):
            thumb = ""
            if ev.jpeg:
                p = self.thumb_path(sid, i)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(encode_jpeg(resize_max_side(decode_jpeg(ev.jpeg), 640), 80))
                thumb = f"/api/search/{sid}/thumb/{i}"
            elif ev.alert_image:
                thumb = "/api/alerts/image/" + ev.alert_image
            info = self.cameras.info(ev.camera)
            start = ev.start
            items.append({"camera": ev.camera, "camera_label": info.get("label") or info.get("name") or ev.camera,
                          "start": round(start, 1), "end": round(max(ev.end, ev.ts, start), 1), "ts": round(ev.ts or start, 1),
                          "time": time.strftime("%m-%d %H:%M:%S", time.localtime(start)),
                          "box": ev.box, "evidence": ev.evidence or (ev.texts[0] if ev.texts else ""),
                          "confidence": ev.confidence or ("高" if ev.verified else ""), "alert_id": ev.alert_id,
                          "thumb": thumb, "clip_start": round(start - 10, 1), "clip_end": round(max(ev.end, start) + 50, 1)})
        what = out["plan"].look_for
        if items:
            brief = "；".join(f"{h['time'][6:11]} {h['camera_label']}" for h in items[:5]) + ("等" if len(items) > 5 else "")
            summary = f"{out['scope']} 找到 {len(items)} 处：{brief}"
        else:
            summary = f"{out['scope']} 没有找到“{what}”"
        p = out["plan"]
        return {"search_id": sid, "question": question, "found": bool(items), "summary": summary, "look_for": what,
                "scope": {"text": out["scope"], "start": p.start, "end": p.end, "cameras": p.cameras, "keywords": p.keywords},
                "hits": items, "note": "；".join(out["notes"]), "candidates": out["candidates"],
                "recording": self.playback is not None}

    async def run(self, question: str, session: str = "web", *, camera: str = "", start: str = "", end: str = "",
                  focus: str | None = None, sid: str | None = None) -> dict[str, Any]:
        sid = sid or self.new_id()
        self._save(sid, session, question, "running", None)
        ev_session = session if not session.startswith("page:") else "page"

        def progress(text: str) -> None:
            self.progress[sid] = text
            self.bus.publish(TOPIC_UI, {"type": "search_progress", "session": ev_session, "search_id": sid, "text": text})
        try:
            out = await self.agent.run(question, camera=camera, start=start, end=end, focus=focus, progress=progress)
            result = self._result(sid, question, out)
            self._save(sid, session, question, "done", result)
            if result["found"]:
                self.open_by_session[session] = sid
            self.bus.publish(TOPIC_UI, {"type": "search_results", "session": ev_session, "search_id": sid, "result": result})
            log.info("检索[%s] %s → %s", session, question, result["summary"])
            return result
        except asyncio.CancelledError:
            self._save(sid, session, question, "cancelled", None)
            self.bus.publish(TOPIC_UI, {"type": "search_progress", "session": ev_session, "search_id": sid, "text": "已取消", "done": True})
            raise
        except Exception as e:
            log.exception("检索失败")
            self._save(sid, session, question, "error", {"error": str(e)})
            raise
        finally:
            self.progress.pop(sid, None)

    def start_run(self, question: str, session: str, **kw: Any) -> str:
        """网页搜索框：后台跑，立即返回编号，网页轮询结果。"""
        sid = self.new_id()
        task = asyncio.create_task(self.run(question, session, sid=sid, **kw))
        self._running[sid] = task
        task.add_done_callback(lambda t: (self._running.pop(sid, None), t.cancelled() or t.exception()))
        return sid

    def cancel(self, sid: str) -> bool:
        t = self._running.get(sid)
        if t and not t.done():
            t.cancel()
            return True
        return False


__all__ = ["SearchService", "SearchIndex"]
