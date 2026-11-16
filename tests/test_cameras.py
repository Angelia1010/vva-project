"""多摄像头、多岗位的测试：摄像头管理、视觉调度、按摄像头的职责与告警、智能体选摄像头。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.fakes import FakeLLM, FakeVLM, make_duties  # noqa: E402
from vva.cameras import CameraHub, CameraStore, SingleFrames  # noqa: E402
from vva.core import turn  # noqa: E402
from vva.core.bus import TOPIC_UI, EventBus  # noqa: E402
from vva.core.sqlite import SqliteDB  # noqa: E402
from vva.core.types import Frame  # noqa: E402
from vva.perception.scheduler import Priority, Superseded, VisionScheduler  # noqa: E402
from vva.perception.timeline import Timeline  # noqa: E402
from vva.station import StationStore  # noqa: E402

YES = json.dumps({"match": True, "evidence": "有人吸烟", "confidence": "高", "box_2d": [100, 300, 300, 900]},
                 ensure_ascii=False)


def _img(v: int = 100) -> np.ndarray:
    return np.full((120, 160, 3), v, np.uint8)


def _hub(n: int = 2, db=None, **kw) -> CameraHub:
    db = db or SqliteDB(":memory:")
    stations = StationStore(db, {"name": "1 号仓库"})
    hub = CameraHub(CameraStore(db), stations, {"seconds": 10, "fps": 5}, **kw)
    names = ["大门", "装卸区", "库房", "办公室"]
    hub.seed([{"name": names[i], "source": {"type": "push"}} for i in range(n)])
    return hub


def _feed(hub: CameraHub, v: int = 100) -> None:
    for c in hub.list():
        hub.buffer(c.id).add(Frame(image=_img(v), ts=time.time(), source_id=c.id))


# ---------------------------------------------------------------- 摄像头管理
def test_hub_seed_resolve_label_and_limit():
    hub = _hub(2, max_cameras=3)
    assert [c.id for c in hub.list()] == ["cam0", "cam1"]
    assert hub.resolve("装卸区").id == "cam1" and hub.resolve("2 号").id == "cam1" and hub.resolve("cam0").id == "cam0"
    assert hub.resolve("1号仓库大门").id == "cam0" and hub.resolve("后门") is None
    assert hub.label("cam1") == "装卸区"                       # 只有一个岗位时不写岗位名
    st = hub.stations.create(name="2 号仓库")
    hub.add(name="门口", station_id=st.id)
    assert hub.label("cam2") == "2 号仓库·门口" and hub.info("cam2")["station"] == "2 号仓库"
    try:
        hub.add(name="第四路")
        raise AssertionError("超过上限应报错")
    except ValueError as e:
        assert "最多" in str(e)
    assert hub.remove("cam2") and hub.get("cam2") is None


def test_hub_seed_from_legacy_video_and_push_status():
    db = SqliteDB(":memory:")
    stations = StationStore(db, {"name": "门口", "area": "大门和走廊"})
    hub = CameraHub(CameraStore(db), stations, {}, browser_mode=True)
    assert hub.seed([], legacy_video={"type": "local", "source_id": "cam0", "local": {"index": 0}}, legacy_area="大门和走廊")
    cam = hub.get("cam0")
    assert cam.type == "local" and cam.area == "大门和走廊"
    hub.start()                                              # 浏览器模式：本机摄像头改为推送
    assert hub.push_ids() == ["cam0"] and hub.status("cam0")["state"] == "waiting"
    ok, _ = cv2_jpeg()
    assert hub.push_jpeg("cam0", ok) and hub.status("cam0")["state"] == "online"
    changed = hub.check_health()
    assert changed and changed[0][1] is True
    hub.stop()
    again = CameraHub(CameraStore(db), stations, {})          # 重启后从库里读，不再重复建
    assert [c.id for c in again.list()] == ["cam0"] and not again.seed([], legacy_video={"type": "push"})


def cv2_jpeg():
    import cv2
    return cv2.imencode(".jpg", _img())[1].tobytes(), None


def test_password_hidden_in_public_source():
    hub = _hub(1)
    hub.update("cam0", source={"type": "stream", "url": "rtsp://admin:secret@10.0.0.2:554/102"})
    assert hub.to_dict(hub.get("cam0"))["source"]["url"] == "rtsp://admin:******@10.0.0.2:554/102"


# ---------------------------------------------------------------- 视觉调度
class SlowVLM:
    def __init__(self) -> None:
        self.order: list[str] = []

    async def ask(self, images, prompt, **kw):
        await asyncio.sleep(0.01)
        self.order.append(prompt)
        return prompt


def test_scheduler_priority_and_supersede():
    async def run():
        vlm = SlowVLM()
        s = VisionScheduler(vlm, max_concurrency=1)
        first = asyncio.ensure_future(s.ask([], "timeline-0", priority=Priority.TIMELINE))
        await asyncio.sleep(0)                               # 第一个已经在跑
        old = asyncio.ensure_future(s.ask([], "observe-old", priority=Priority.DUTY, key="observe:cam1"))
        late = asyncio.ensure_future(s.ask([], "timeline-1", priority=Priority.TIMELINE))
        user = asyncio.ensure_future(s.ask([], "user", priority=Priority.USER))
        await asyncio.sleep(0)
        new = asyncio.ensure_future(s.ask([], "observe-new", priority=Priority.DUTY, key="observe:cam1"))
        await asyncio.gather(first, late, user, new)
        try:
            await old
            raise AssertionError("旧任务应被替换")
        except Superseded:
            pass
        assert vlm.order == ["timeline-0", "user", "observe-new", "timeline-1"]
        assert s.stats()["dropped"] == 1
    asyncio.run(run())


# ---------------------------------------------------------------- 后台观察、职责
def test_observer_only_judges_duties_for_its_camera():
    from vva.perception.observer import Observer
    hub = _hub(2)
    _feed(hub)
    m = make_duties(buffer=hub)
    m.book.create(name="禁止吸烟", condition="有人吸烟")                    # 所有摄像头
    m.book.create(name="装卸区戴安全帽", condition="有人没戴安全帽", cameras=["cam1"])
    obs0 = Observer(hub.buffer("cam0"), FakeVLM(), Timeline(), m.book, EventBus(), camera_id="cam0", camera_name="大门")
    obs1 = Observer(hub.buffer("cam1"), FakeVLM(), Timeline(), m.book, EventBus(), camera_id="cam1", camera_name="装卸区")
    assert [d.name for d in obs0._watching()] == ["禁止吸烟"]
    assert [d.name for d in obs1._watching()] == ["禁止吸烟", "装卸区戴安全帽"]
    assert "安全帽" not in obs0.build_prompt() and "安全帽" in obs1.build_prompt()


def test_observer_reports_camera_on_hit_and_timeline():
    from vva.perception.observer import Observer
    hub = _hub(2)
    _feed(hub)
    m = make_duties(buffer=hub)
    d = m.book.create(name="禁止吸烟", condition="有人吸烟")
    hits = []
    vlm = FakeVLM(default=json.dumps({"summary": "门口有人抽烟", "people_count": 1,
                                      "triggered_rules": [{"id": d.id, "evidence": "手里有烟"}]}, ensure_ascii=False))
    tl = Timeline()
    obs = Observer(hub.buffer("cam1"), vlm, tl, m.book, EventBus(), camera_id="cam1", camera_name="装卸区",
                   on_hit=lambda *a, **kw: hits.append((a[0], kw.get("camera"))))
    entry = asyncio.run(obs.step())
    assert entry.camera == "cam1" and "[装卸区]" in entry.to_text() and hits == [(d.id, "cam1")]
    assert tl.latest("cam1") is entry and tl.latest("cam0") is None and tl.query(5, "cam0") == []


def test_station_pause_only_stops_its_cameras():
    from vva.perception.observer import Observer
    hub = _hub(2)
    st2 = hub.stations.create(name="2 号仓库", paused=True)
    hub.update("cam1", station_id=st2.id)
    m = make_duties(buffer=hub)
    m.book.create(name="禁止吸烟", condition="有人吸烟")
    mk = lambda cid: Observer(hub.buffer(cid), FakeVLM(), Timeline(), m.book, EventBus(), camera_id=cid,  # noqa: E731
                              paused=lambda: hub.station_of(cid).paused)
    assert mk("cam0")._watching() and not mk("cam1")._watching()
    assert not hub.stations.all_paused()


def test_engine_cooldown_and_alert_per_camera():
    async def run():
        hub = _hub(2)
        _feed(hub)
        m = make_duties(buffer=hub, vlm=FakeVLM(answers={"要复核的情况": YES, "放大图": YES}))
        m.verifier.delay = 0
        m.camera_info = hub.info
        d = m.book.create(name="禁止吸烟", condition="有人吸烟", targets=["web"])
        assert m.engine.on_hit(d.id, "疑似吸烟", time.time(), b"x", camera="cam0")
        await asyncio.sleep(0.05)
        assert m.engine.on_hit(d.id, "疑似吸烟", time.time(), b"x", camera="cam1")   # 另一路照样报
        await asyncio.sleep(0.05)
        assert not m.engine.on_hit(d.id, "又吸烟", time.time(), b"x", camera="cam0")  # 同一路冷却中
        alerts = m.alerts.store.query()
        assert sorted(a.camera for a in alerts) == ["cam0", "cam1"]
        a1 = next(a for a in alerts if a.camera == "cam1")
        assert a1.camera_name == "装卸区" and a1.station == "1 号仓库" and a1.where == "1 号仓库·装卸区"
        assert [a.camera for a in m.alerts.store.query(camera="cam1")] == ["cam1"]
        assert m.alerts.camera_today()["cam0"]["today"] == 1
        scoped = m.book.create(name="装卸区", condition="有人没戴安全帽", cameras=["cam1"])
        assert not m.engine.on_hit(scoped.id, "没戴", time.time(), b"x", camera="cam0")   # 这条职责不管大门
    asyncio.run(run())


def test_inspect_runs_on_each_camera():
    async def run():
        hub = _hub(2)
        _feed(hub)
        vlm = FakeVLM(default=json.dumps({"result": "压力 0.4", "abnormal": False, "evidence": ""}, ensure_ascii=False))
        m = make_duties(buffer=hub, vlm=vlm)
        m.scheduler.vlm = vlm
        m.camera_info = hub.info
        m.camera_options = lambda: [{"id": c.id, "name": c.name, "label": hub.label(c.id)} for c in hub.list()]
        d = m.book.create(name="读压力表", condition="压力表读数", kind="inspect", times=["09:00"])
        await m.scheduler.run_duty(d)
        recs = m.alerts.store.query(statuses=("record",))
        assert sorted(a.camera for a in recs) == ["cam0", "cam1"] and "大门：压力 0.4" in d.last_result
        one = m.book.create(name="只看库房", condition="压力表读数", kind="inspect", times=["09:00"], cameras=["cam1"])
        assert m.camera_text(one) == "装卸区" and m.scheduler.cameras_for(one) == ["cam1"]
        r = await m.test(one)
        assert r["camera"] == "cam1"
    asyncio.run(run())


# ---------------------------------------------------------------- 智能体
def test_vision_tools_pick_camera():
    async def run():
        from vva.agent.tools import VisionTools
        hub = _hub(2)
        _feed(hub)
        tools = VisionTools(hub, FakeVLM(default="依据：x\n结论：有 1 人\n把握：高"), Timeline(), None, EventBus())
        turn.begin("dingtalk:c1")                               # 钉钉里没有“正在看的那一路”
        r = await tools.tool_look_now("几个人")
        assert "请先问用户" in r["error"]
        r = await tools.tool_look_now("几个人", camera="装卸区")
        assert r["camera"] == "装卸区" and "有 1 人" in json.dumps(r, ensure_ascii=False)
        r = await tools.tool_look_now("几个人", camera="全部")
        assert [x["camera"] for x in r["cameras"]] == ["大门", "装卸区"]
        turn.begin("web", "cam1")                               # 值守台正在看装卸区
        r = await tools.tool_look_now("几个人")
        assert r["camera"] == "装卸区"
        r = await tools.tool_look_now("几个人", camera="后门")
        assert "没有找到" in r["error"]
        single = VisionTools(SingleFrames(hub.buffer("cam0")), FakeVLM(), Timeline(), None, EventBus())
        turn.begin("dingtalk:c1")
        assert "camera" not in await single.tool_look_now("几个人")   # 只有一路时不提摄像头
    asyncio.run(run())


def test_memory_events_by_camera(tmp_path):
    from vva.memory.store import KIND_COMMAND, KIND_OBSERVATION, MemoryEvent, MemoryStore
    store = MemoryStore(str(tmp_path / "m.db"))
    now = time.time()
    store.add_event(MemoryEvent(ts=now - 30, kind=KIND_OBSERVATION, summary="大门有人", camera="cam0", frame="a.jpg"))
    store.add_event(MemoryEvent(ts=now - 20, kind=KIND_OBSERVATION, summary="装卸区有车", camera="cam1", frame="b.jpg"))
    store.add_event(MemoryEvent(ts=now - 10, kind=KIND_COMMAND, summary="操作员指令：看看"))
    assert [e.summary for e in store.events_between(now - 60, now, camera="cam1")] == ["装卸区有车", "操作员指令：看看"]
    assert store.nearest_frame(now - 30, 60, camera="cam1").summary == "装卸区有车"
    store.close()
    again = MemoryStore(str(tmp_path / "m.db"))                 # 重开（列已存在）不报错
    assert again.count_events() == 3


# ---------------------------------------------------------------- 应用
def test_app_multi_camera_wiring(tmp_path):
    async def run():
        from tests.test_channels import _app
        app = _app(tmp_path, FakeLLM([]), vlm=FakeVLM(answers={"要复核的情况": YES, "放大图": YES}),
                   observer={"enabled": True, "interval": 30})
        assert [c.id for c in app.cameras.list()] == ["cam0"]          # 旧配置自动变成第一路
        st2 = app.station.create(name="2 号仓库", scene="仓库内禁止吸烟")
        app.cameras.add(name="门口", station_id=st2.id, area="出入口", source={"type": "push"})
        await app.start()
        _feed(app.cameras)
        app.set_focus("cam1")
        ui = app.bus.subscribe(TOPIC_UI)
        prompt = app._extra_prompt()
        assert "岗位与摄像头" in prompt and "cam1 门口" in prompt and "2 号仓库" in prompt
        turn.begin("web", "cam1")
        assert "看的是：cam1" in app.cameras_prompt()
        assert "出入口" in app.scene_text("cam1") and "禁止吸烟" in app.scene_text("cam1")
        assert app.snapshot(camera="cam1") is not None
        app.duties.verifier.delay = 0
        d = app.duties.book.create(name="禁止吸烟", condition="有人吸烟", targets=["web"])
        app.duties.engine.on_hit(d.id, "疑似吸烟", time.time(), b"\xff\xd8x", camera="cam1")
        await asyncio.sleep(0.1)
        alert = app.duties.alerts.store.query()[0]
        assert alert.camera == "cam1" and alert.where == "2 号仓库·门口"
        evs = [e for e in _drain(ui) if e["type"] == "alert"]
        assert evs and "门口" in evs[0]["text"]
        st = app.status()
        assert len(st["cameras"]) == 2 and st["focus_camera"] == "cam1" and "max_concurrency" in st["vision"]
        assert app.observers and set(app.observers.observers) == {"cam0", "cam1"}
        app.cameras.remove("cam1")
        assert set(app.observers.observers) == {"cam0"}
        await app.stop()
    asyncio.run(run())


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_actions_snapshot_by_camera(tmp_path):
    from vva.actions import ActionService
    hub = _hub(2)
    _feed(hub)
    shots = []

    def snap(camera=None):
        shots.append(camera)
        return b"jpeg"
    s = ActionService(SqliteDB(":memory:"), EventBus(), shots_dir=str(tmp_path), snapshot=snap)
    s.camera_info = hub.info
    r = asyncio.run(s.run(3, camera="cam1"))
    assert r["ok"] and shots == ["cam1"] and "装卸区" in r["detail"] and s.store.runs(1)[0]["camera"] == "cam1"


def test_camera_api(tmp_path):
    import pytest
    pytest.importorskip("fastapi")
    import cv2
    from fastapi.testclient import TestClient

    from tests.test_channels import _app
    from vva.web.server import create_web_app
    app = _app(tmp_path, FakeLLM([]), web={"admin_token": "pw"})
    with TestClient(create_web_app(app)) as c:
        h = {"X-Token": "pw"}
        r = c.get("/api/cameras").json()
        assert r["cameras"][0]["id"] == "cam0" and r["max"] == 4
        assert c.post("/api/cameras", json={"name": "x"}).status_code == 401
        st = c.post("/api/stations", headers=h, json={"name": "2 号车间", "owner": "李四"}).json()
        cam = c.post("/api/cameras", headers=h, json={"name": "门口", "station_id": st["id"], "source": {"type": "push"}}).json()
        assert cam["id"] == "cam1" and cam["label"] == "2 号车间·门口"
        assert c.post("/api/cameras", headers=h, json={"name": "y", "source": {"type": "stream", "url": "ftp://x"}}).status_code == 400
        jpeg = cv2.imencode(".jpg", _img())[1].tobytes()
        assert c.post("/api/ingest/frame?cam=cam1", content=jpeg).json()["ok"]
        assert c.post("/api/ingest/frame?cam=nope", content=jpeg).status_code == 404
        assert c.get("/api/cameras/cam1/snapshot.jpg").status_code == 200
        assert c.get("/api/snapshot.jpg?cam=cam1").status_code == 200
        assert "cam1" in c.get("/api/capabilities").json()["push_cameras"]
        d = c.post("/api/duties", headers=h, json={"name": "门口吸烟", "condition": "有人吸烟", "cameras": ["cam1"]}).json()
        assert d["cameras"] == ["cam1"]
        assert c.post("/api/duties", headers=h, json={"condition": "x", "cameras": ["camX"]}).status_code == 400
        ov = c.get("/api/overview").json()
        assert len(ov["cameras"]) == 2 and len(ov["stations"]) == 2
        assert ov["duties"][0]["camera_text"] == "2 号车间·门口"
        upd = c.put("/api/cameras/cam1", headers=h, json={"enabled": False,
                                                           "source": {"type": "stream", "url": "rtsp://a:pw@1.2.3.4/1"}}).json()
        assert "******" in upd["source"]["url"]
        c.put("/api/cameras/cam1", headers=h, json={"source": upd["source"]})          # 密码没改，原样提交
        assert app.cameras.get("cam1").source["url"] == "rtsp://a:pw@1.2.3.4/1"
        assert c.get("/api/alerts?camera=cam1").json()["alerts"] == []
        assert c.delete(f"/api/stations/{st['id']}", headers=h).status_code == 400   # 下面还有摄像头
        assert c.delete("/api/cameras/cam1", headers=h).json()["duties_affected"] == ["门口吸烟"]
        assert c.delete("/api/cameras/cam0", headers=h).status_code == 400           # 至少保留一路
        assert c.delete(f"/api/stations/{st['id']}", headers=h).json()["ok"]
        assert c.get("/settings").status_code == 200
