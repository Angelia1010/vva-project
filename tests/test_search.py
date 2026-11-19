"""视频检索的测试：索引切词与查询、候选合并、搜索子智能体全流程、主智能体工具、开关。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import cv2
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.fakes import FakeLLM, FakeVLM, make_duties  # noqa: E402
from vva.cameras import CameraHub, CameraStore  # noqa: E402
from vva.core import turn  # noqa: E402
from vva.core.bus import TOPIC_OBSERVATION, TOPIC_UI, EventBus  # noqa: E402
from vva.core.sqlite import SqliteDB  # noqa: E402
from vva.perception.timeline import TimelineEntry  # noqa: E402
from vva.search import SearchService  # noqa: E402
from vva.search.index import Query, SearchIndex, grams  # noqa: E402
from vva.station import StationStore  # noqa: E402

JPEG = cv2.imencode(".jpg", np.full((180, 320, 3), 120, np.uint8))[1].tobytes()
YES = json.dumps({"match": True, "evidence": "左侧男子手里夹着香烟", "confidence": "高", "box_2d": [100, 200, 400, 900]},
                 ensure_ascii=False)
NO = json.dumps({"match": False, "evidence": "手里是笔", "confidence": "高", "box_2d": None}, ensure_ascii=False)


def _plan(**kw):
    t = time.localtime(time.time() - 7200)
    d = {"start": time.strftime("%Y-%m-%d %H:%M", t), "end": time.strftime("%Y-%m-%d %H:%M"), "cameras": [],
         "keywords": ["吸烟", "抽烟", "香烟"], "people_min": None, "look_for": "有人在吸烟"}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


def _env(tmp_path, llm_script=None, vlm=None):
    db = SqliteDB(":memory:")
    hub = CameraHub(CameraStore(db), StationStore(db, {"name": "1 号仓库"}), {})
    hub.seed([{"name": "大门", "source": {"type": "push"}}, {"name": "库房", "source": {"type": "push"}}])
    duties = make_duties(db_path=":memory:")
    bus = EventBus()

    async def frame_ok(cam, t):
        return JPEG, t
    svc = SearchService({"dir": str(tmp_path / "search"), "budget": {"max_candidates": 10, "max_frames": 16, "max_hits": 8,
                                                                       "timeout_s": 20}},
                        db, hub, bus, FakeLLM(llm_script or [_plan()]), vlm or FakeVLM(default=YES), duties.alerts.store)
    svc._frame = frame_ok
    svc.agent.frame = frame_ok
    return svc, hub, duties


# ---------------------------------------------------------------- 索引
def test_grams_and_index_query():
    assert grams("有人吸烟 OK2") == "有人 人吸 吸烟 ok2"
    idx = SearchIndex(SqliteDB(":memory:"))
    now = time.time()
    idx.add("cam0", now - 100, "门口有一名男子在抽烟", 1)
    idx.add("cam0", now - 50, "叉车经过通道", 0)
    idx.add("cam1", now - 30, "两人站在货架旁，其中一人吸烟", 2)
    hits = idx.query(Query(now - 3600, now, keywords=["吸烟", "抽烟"]))
    assert [h.text[:2] for h in hits] == ["门口", "两人"] and hits[1].matched == ["吸烟"]
    assert [h.camera for h in idx.query(Query(now - 3600, now, cameras=["cam1"], keywords=["吸烟"]))] == ["cam1"]
    assert len(idx.query(Query(now - 3600, now, people_min=2))) == 1
    assert idx.query(Query(now - 3600, now, keywords=["叉"]))[0].text.startswith("叉车")   # 单字退回 LIKE
    assert idx.delete_before(now - 60) == 1 and idx.count() == 2


# ---------------------------------------------------------------- 粗筛
def test_candidates_merge_records_and_alerts(tmp_path):
    svc, hub, duties = _env(tmp_path)
    now = time.time()
    for i in range(4):                                          # 同一件事连着记了 4 条：合并成一个事件
        svc.index.add("cam0", now - 600 + i * 20, "门口有人在抽烟")
    svc.index.add("cam1", now - 300, "库房里有人吸烟")
    d = duties.book.create(name="禁止吸烟", condition="有人吸烟")
    duties.alerts.create(d, "库房有人手持香烟", [], camera="cam1", ts=now - 290)
    plan = asyncio.run(svc.agent.plan("最近两小时有没有人抽烟"))
    evs = svc.agent.candidates(plan)
    assert len(evs) == 2
    assert evs[0].camera == "cam1" and evs[0].alert_id                     # 有告警的排第一
    assert evs[1].end - evs[1].start == pytest.approx(60, abs=1)


def test_plan_falls_back_to_last_24h_and_resolves_camera(tmp_path):
    svc, _, _ = _env(tmp_path, [json.dumps({"start": "乱写", "cameras": ["库房"], "keywords": []}, ensure_ascii=False)])
    p = asyncio.run(svc.agent.plan("库房有没有叉车"))
    assert 86000 < p.end - p.start < 86500 and p.cameras == ["cam1"] and p.keywords       # 关键词取自问题


# ---------------------------------------------------------------- 全流程
def test_search_run_verifies_refines_and_saves(tmp_path):
    async def run():
        refine = json.dumps({"first": 2})
        vlm = FakeVLM(answers={"从第几张开始": refine, "要确认的情况": YES})
        svc, _, _ = _env(tmp_path, vlm=vlm)
        now = time.time()
        svc.index.add("cam0", now - 600, "门口有人抽烟")
        svc.index.add("cam1", now - 300, "库房有人吸烟")
        ui = svc.bus.subscribe(TOPIC_UI)
        r = await svc.run("最近两小时有人抽烟吗", "web")
        assert r["found"] and len(r["hits"]) == 2 and r["hits"][0]["camera"] == "cam0"       # 按时间排
        h = r["hits"][0]
        assert h["box"] and h["evidence"] == "左侧男子手里夹着香烟" and h["thumb"].endswith("/thumb/0")
        assert h["start"] == pytest.approx(now - 615, abs=2)                 # 精确定位：第 2 张（前 15 秒）开始出现
        assert max(n for n, _ in vlm.requests) <= 4                           # 一次最多 4 张图
        assert os.path.isfile(svc.thumb_path(r["search_id"], 0))
        assert "找到 2 处" in r["summary"] and svc.get(r["search_id"])["status"] == "done"
        types = [e["type"] for e in _drain(ui)]
        assert "search_progress" in types and types[-1] == "search_results"
        assert svc.open_by_session["web"] == r["search_id"] and svc.history()[0]["hits"] == 2
    asyncio.run(run())


def test_search_rejects_false_candidates(tmp_path):
    async def run():
        svc, _, _ = _env(tmp_path, vlm=FakeVLM(default=NO))
        svc.index.add("cam0", time.time() - 600, "门口有人抽烟")
        r = await svc.run("有人抽烟吗", "web")
        assert not r["found"] and "没有找到" in r["summary"] and "web" not in svc.open_by_session
    asyncio.run(run())


def test_search_without_frames_keeps_alerts_unverified(tmp_path):
    async def run():
        svc, _, duties = _env(tmp_path)

        async def no_frame(cam, t):
            return None, t
        svc.agent.frame = no_frame
        d = duties.book.create(name="禁止吸烟", condition="有人吸烟")
        duties.alerts.create(d, "门口有人吸烟", [], camera="cam0", ts=time.time() - 100)
        r = await svc.run("有人抽烟吗", "dingtalk:c1")
        assert r["found"] and r["hits"][0]["confidence"] == "未看图确认" and r["hits"][0]["alert_id"]
    asyncio.run(run())


def test_budget_limits_frames(tmp_path):
    async def run():
        svc, _, _ = _env(tmp_path, vlm=FakeVLM(default=NO))
        svc.agent.max_frames = 3
        now = time.time()
        for i in range(8):
            svc.index.add(f"cam{i % 2}", now - 3000 + i * 300, "有人抽烟")
        r = await svc.run("有人抽烟吗", "web")
        assert "只确认了前 3 个" in r["note"]
    asyncio.run(run())


# ---------------------------------------------------------------- 工具、开关
def test_tools_visibility_control_and_switch(tmp_path):
    async def run():
        svc, _, _ = _env(tmp_path, vlm=FakeVLM(answers={"从第几张开始": json.dumps({"first": 0})}, default=YES))
        svc.index.add("cam0", time.time() - 600, "门口有人抽烟")
        turn.begin("web", "cam0")
        names = [s["function"]["name"] for s in svc.tools.schemas()]
        assert names == ["search_video"]                                    # 还没有打开的结果
        r = json.loads(await svc.tools.call("search_video", {"question": "刚才有人抽烟吗"}))
        assert r["found"] and r["shown"] and r["hits"][0]["no"] == 1
        assert "control_search_result" in [s["function"]["name"] for s in svc.tools.schemas()]
        ui = svc.bus.subscribe(TOPIC_UI)
        r = json.loads(await svc.tools.call("control_search_result", {"action": "zoom", "index": 1}))
        assert r["ok"] and _drain(ui)[-1]["action"] == "zoom"
        assert "error" in json.loads(await svc.tools.call("control_search_result", {"action": "play", "index": 5}))
        svc.set_enabled(False)
        assert svc.tools.schemas() == [] and svc.prompt_block() == ""
        svc.set_enabled(True)
        svc.cfg["channels"] = ["web"]
        turn.begin("dingtalk:c1")
        assert svc.tools.schemas() == []                                    # 钉钉里不开放
    asyncio.run(run())


def test_indexer_and_history_import(tmp_path):
    async def run():
        from vva.memory import LongTermMemory
        from vva.memory.store import KIND_OBSERVATION, MemoryEvent
        svc, _, _ = _env(tmp_path)
        mem = LongTermMemory({"dir": str(tmp_path / "mem")}, svc.bus, FakeLLM([]), FakeVLM())
        mem.store.add_event(MemoryEvent(ts=time.time() - 100, kind=KIND_OBSERVATION, summary="叉车经过", camera="cam1",
                                        details={"camera_name": "库房"}))
        svc.memory = mem
        svc.bus.bind_loop(asyncio.get_running_loop())
        svc.start()
        await asyncio.sleep(0.05)
        assert svc.index.count() == 1
        svc.bus.publish(TOPIC_OBSERVATION, {"entry": TimelineEntry(ts=time.time(), summary="门口有人抽烟", people_count=1,
                                                                   activities=["抽烟"], camera="cam0", camera_name="大门"),
                                            "jpeg": JPEG})
        await asyncio.sleep(0.05)
        hits = svc.index.query(Query(time.time() - 60, time.time() + 1, keywords=["抽烟"]))
        assert hits and hits[0].camera_name == "大门" and "动作：抽烟" in hits[0].text
        await svc.stop()
        await mem.close()
    asyncio.run(run())


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_app_wires_search(tmp_path):
    from tests.test_channels import _app
    app = _app(tmp_path, FakeLLM([]))
    assert app.search is not None and "search_video" in [s["function"]["name"] for s in app.tools.schemas()]
    assert "录像检索" in app._extra_prompt()


def test_search_api(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from tests.test_channels import _app
    from vva.web.server import create_web_app
    app = _app(tmp_path, FakeLLM([_plan()]), vlm=FakeVLM(default=YES), web={"admin_token": "pw"})
    app.search.index.add("cam0", time.time() - 600, "门口有人抽烟")

    async def frame(camera, t):                 # 测试环境没有录像和关键画面，给一张固定画面
        return JPEG, t
    app.search.agent.frame = frame
    with TestClient(create_web_app(app)) as c:
        assert c.post("/api/search", json={"question": ""}).status_code == 400
        sid = c.post("/api/search", json={"question": "有人抽烟吗"}).json()["search_id"]
        for _ in range(50):
            r = c.get(f"/api/search/{sid}").json()
            if r["status"] != "running":
                break
            time.sleep(0.1)
        assert r["status"] == "done" and r["result"]["found"]
        assert c.get("/api/search/history").json()["history"][0]["id"] == sid
        assert c.put("/api/search/settings", json={"enabled": False}).status_code == 401
        assert c.put("/api/search/settings", headers={"X-Token": "pw"}, json={"enabled": False}).json()["enabled"] is False
        assert c.post("/api/search", json={"question": "x"}).status_code == 403


def test_pick_evenly_and_client_cap():
    from vva.models.clients import pick_evenly
    assert pick_evenly(list(range(8)), 4) == [0, 2, 5, 7] and pick_evenly([1, 2], 4) == [1, 2]
    assert pick_evenly(list(range(5)), 1) == [4]
