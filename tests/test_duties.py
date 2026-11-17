"""职责、告警、通知路由的测试（假模型，内存数据库）。"""
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
from vva.core import turn  # noqa: E402
from vva.core.bus import TOPIC_ALERT, TOPIC_UI, EventBus  # noqa: E402
from vva.core.sqlite import SqliteDB  # noqa: E402
from vva.core.types import Frame  # noqa: E402
from vva.duties.model import Duty, parse_hours  # noqa: E402
from vva.duties.scheduler import due_time  # noqa: E402
from vva.duties.store import DutyBook, DutyStore  # noqa: E402
from vva.duties.tools import end_of_task, targets_for_session  # noqa: E402
from vva.video.frame_buffer import FrameBuffer  # noqa: E402

YES = json.dumps({"match": True, "evidence": "左下角男子手里夹着冒烟的香烟", "confidence": "高",
                  "box_2d": [100, 300, 300, 900]}, ensure_ascii=False)
NO = json.dumps({"match": False, "evidence": "手里拿的是笔", "confidence": "高", "box_2d": None}, ensure_ascii=False)


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _at(h, m):
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1))


def _buffer():
    buf = FrameBuffer()
    buf.add(Frame(image=np.full((360, 640, 3), 90, np.uint8), ts=time.time()))
    return buf


# ---------------------------------------------------------------- 模型与清单
def test_schedule_and_active():
    assert parse_hours("08:00-18:00, 22:00-06:00") == [(480, 1080), (1320, 360)]
    night = Duty(id="d1", name="夜间闯入", condition="有人", hours="22:00-06:00")
    assert night.in_schedule(_at(23, 30)) and night.in_schedule(_at(5, 0)) and not night.in_schedule(_at(12, 0))
    work = Duty(id="d2", name="离岗", condition="没人", hours="08:00-18:00", weekdays=[1, 2, 3, 4, 5])
    assert work.schedule_text() == "工作日 08:00-18:00"
    tmp = Duty(id="d3", name="快递", condition="快递", temporary=True, expires=time.time() - 1)
    assert tmp.expired() and not tmp.active()
    assert Duty(id="d4", name="报告", condition="x", kind="report").active() is False      # 只有盯守类由后台观察判断


def test_book_persists_logs_and_imports_legacy(tmp_path):
    db = SqliteDB(str(tmp_path / "ops.db"))
    book = DutyBook(DutyStore(db))
    assert book.load([{"id": "r1", "condition": "画面中没有人", "cooldown_s": 120}]) == 1
    d = book.create(actor="管理员", name="禁止吸烟", condition="有人吸烟", targets=["dingtalk:cidA"], level="urgent")
    book.update(d.id, actor="管理员", enabled=False)
    book2 = DutyBook(DutyStore(db))
    assert [x.name for x in book2.list()] == ["画面中没有人", "禁止吸烟"]
    assert book2.get(d.id).enabled is False and book2.get("d1").source == "legacy"
    assert book2.create(condition="x").id == "d3"                                    # 编号接着往后排
    assert [c["action"] for c in DutyStore(db).changes(d.id)] == ["修改", "添加"]
    assert [x.id for x in book2.active()] == ["d1", "d3"]
    book2.paused = True
    assert book2.active() == []


# ---------------------------------------------------------------- 发现 → 复核 → 告警 → 通知
def test_engine_verifies_then_alerts_and_routes():
    async def run():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        alerts_q, ui = bus.subscribe(TOPIC_ALERT), bus.subscribe(TOPIC_UI)
        vlm = FakeVLM(answers={"要复核的情况": YES, "放大图": YES})
        m = make_duties(bus, _buffer(), vlm)
        d = m.book.create(name="禁止吸烟", condition="有人吸烟", targets=["dingtalk:cidA", "web"], level="urgent",
                          voice_text="仓库内禁止吸烟", rule_ref="《仓库安全管理规定》第 5 条")
        assert m.engine.on_hit(d.id, "疑似吸烟", time.time(), b"\xff\xd8first")
        assert not m.engine.on_hit(d.id, "疑似吸烟", time.time(), b"x")                 # 复核期间重复命中忽略
        await asyncio.sleep(0.05)
        msg = _drain(alerts_q)[0]
        assert msg["targets"] == ["dingtalk:cidA", "web"] and msg["name"] == "禁止吸烟"
        assert "冒烟的香烟" in msg["evidence"] and "第 5 条" in msg["evidence"] and msg["voice_text"] == "仓库内禁止吸烟"
        alert = m.alerts.store.get(msg["alert_id"])
        assert alert.status == "pending" and alert.box and [i["caption"] for i in alert.images] == ["初次发现", "放大复核"]
        events = [e["text"] for e in m.alerts.store.events(alert.id)]
        assert events[0].startswith("发现") and events[1].startswith("已通知")
        assert any(e["type"] == "alert_new" for e in _drain(ui))
        assert not m.engine.on_hit(d.id, "又吸烟", time.time(), b"x")                    # 冷却期内不再报
        assert m.status_list()[0]["today_count"] == 1
    asyncio.run(run())


def test_engine_rejects_unverified_trial_and_duration():
    async def run():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        alerts_q = bus.subscribe(TOPIC_ALERT)
        m = make_duties(bus, _buffer(), FakeVLM(answers={"要复核的情况": NO}))
        d = m.book.create(name="禁止吸烟", condition="有人吸烟")
        m.engine.on_hit(d.id, "疑似", time.time(), b"x")
        await asyncio.sleep(0.05)
        assert _drain(alerts_q) == [] and m.alerts.store.query() == []                  # 复核不成立：不告警
        assert d.id not in [x.id for x in m.book.active()]                               # 短暂不再判断
        trial = m.book.create(name="安全帽", condition="没戴安全帽", mode="trial", verify=False)
        m.engine.on_hit(trial.id, "头顶无安全帽", time.time(), b"x")
        await asyncio.sleep(0.02)
        assert _drain(alerts_q) == [] and m.alerts.store.query()[0].status == "trial"   # 试运行只记录
        slow = m.book.create(name="离岗", condition="没人", verify=False, min_duration_s=0.05)
        assert not m.engine.on_hit(slow.id, "空", time.time(), b"x")                     # 第一次只计时
        await asyncio.sleep(0.06)
        assert m.engine.on_hit(slow.id, "空", time.time(), b"x")                         # 持续够久才算
    asyncio.run(run())


def test_alert_actions_and_stats():
    m = make_duties()
    d = m.book.create(name="禁止吸烟", condition="有人吸烟")
    a = m.alerts.create(d, "有人吸烟", [("初次发现", b"\xff\xd8x")])
    b = m.alerts.create(d, "又有人吸烟", [])
    m.alerts.act(a.id, "resolve", "张三", "已劝阻")
    m.alerts.act(b.id, "false", "李四")
    detail = m.alerts.detail(a.id)
    assert detail["status"] == "resolved" and detail["handler"] == "张三" and "已劝阻" in detail["events"][-1]["text"]
    st = m.alerts.stats()
    assert st["today"] == 2 and st["pending"] == 0 and st["false_rate_week"] == 0.5
    assert m.alerts.store.image_path("../ops.db") is None
    img = m.alerts.store.image_path(detail["images"][0]["file"])
    assert img
    assert m.alerts.delete([a.id, b.id, 999]) == 2                                 # 删除：记录、处理经过、截图一起删
    assert m.alerts.store.get(a.id) is None and m.alerts.store.events(a.id) == [] and not os.path.exists(img)


# ---------------------------------------------------------------- 巡检、汇报、临时任务
def test_scheduler_inspect_and_report():
    async def run():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        q = bus.subscribe(TOPIC_ALERT)
        vlm = FakeVLM(answers={"巡检项目": json.dumps({"result": "读数 0.72MPa", "abnormal": True, "evidence": "超过 0.6"},
                                                     ensure_ascii=False)})
        m = make_duties(bus, _buffer(), vlm)
        ins = m.book.create(name="压力表巡检", kind="inspect", condition="读压力表，超过 0.6MPa 为异常", times=["09:00"],
                            targets=["feishu:oc_1"])
        rep = m.book.create(name="每日报告", kind="report", condition="汇总", times=["18:00"], targets=["dingtalk:cidR"])
        ins.last_run = rep.last_run = _at(8, 0)
        assert due_time(ins, _at(9, 1)) and not due_time(ins, _at(8, 59)) and not due_time(ins, _at(9, 30))
        await m.scheduler.run_duty(ins, _at(9, 1))
        await m.scheduler.run_duty(rep, _at(18, 0))
        msgs = _drain(q)
        assert msgs[0]["kind"] == "alert" and "0.72MPa" in msgs[0]["evidence"] and msgs[0]["targets"] == ["feishu:oc_1"]
        assert msgs[1]["kind"] == "report" and msgs[1]["text"] == "今天共 2 次提醒。" and msgs[1]["targets"] == ["dingtalk:cidR"]
        assert ins.last_result == "读数 0.72MPa" and not due_time(ins, _at(9, 2))
    asyncio.run(run())


def test_duty_tools_temporary_tasks():
    async def run():
        m = make_duties()
        assert targets_for_session("web") == ["web", "voice"] and targets_for_session("dingtalk:cid1") == ["dingtalk:cid1", "web"]
        assert end_of_task(until="23:59") > time.time()

        async def in_turn():
            turn.begin("dingtalk:cid1")
            return json.loads(await m.tools.call("add_task", {"condition": "有快递员到门口", "name": "快递提醒"}))
        r = await asyncio.create_task(in_turn())
        d = m.book.get(r["id"])
        assert d.temporary and d.targets == ["dingtalk:cid1", "web"] and d.source == "dingtalk"
        m.book.create(name="禁止吸烟", condition="有人吸烟")
        listed = json.loads(await m.tools.call("list_duties", {}))
        assert [x["name"] for x in listed["duties"]] == ["禁止吸烟"] and listed["temporary"][0]["name"] == "快递提醒"
        assert "error" in json.loads(await m.tools.call("remove_task", {"duty_id": "禁止吸烟"}))   # 长期职责不能用语音删
        assert json.loads(await m.tools.call("pause_duty", {"duty_id": "禁止吸烟"}))["ok"]
        assert json.loads(await m.tools.call("remove_task", {"duty_id": "all"}))["removed"] == ["快递提醒"]
        assert "我负责" not in m.prompt_block() and "禁止吸烟" in m.prompt_block() and "已暂停" in m.prompt_block()
        q = json.loads(await m.tools.call("query_alerts", {"duty": "禁止吸烟"}))
        assert q["count"] == 0
    asyncio.run(run())


def test_parse_duty_draft():
    async def run():
        reply = json.dumps({"name": "禁止吸烟", "kind": "watch", "condition": "画面中有人吸烟", "level": "urgent",
                            "targets": ["dingtalk:cidA", "bogus"], "hours": "", "weekdays": [], "times": [],
                            "min_duration_s": "x", "cooldown_s": 600, "questions": []}, ensure_ascii=False)
        m = make_duties(llm=FakeLLM([reply, "不是 JSON"]))
        m.target_options = lambda: [{"id": "dingtalk:cidA", "label": "钉钉·安全群"}]
        r = await m.parse("仓库里有人吸烟就发钉钉安全群，紧急", [])
        assert r["draft"]["targets"] == ["dingtalk:cidA"] and r["draft"]["level"] == "urgent"
        assert r["draft"]["min_duration_s"] == 0 and r["hints"]                       # 香烟是小目标，有能力提示
        r2 = await m.parse("门口有人就告诉我", [])
        assert r2["draft"]["condition"] == "门口有人就告诉我" and r2["questions"]        # 模型失败时退回原话，并追问通知对象
    asyncio.run(run())


# ---------------------------------------------------------------- 接入入口按通知对象推送
def test_channel_routes_by_targets():
    from tests.test_channels import FakeAPI, FakeHost, FakeReceiver
    from vva.channels.feishu.channel import FeishuChannel

    async def run():
        host = FakeHost()
        host.bus.bind_loop(asyncio.get_running_loop())
        api = FakeAPI()
        ch = FeishuChannel({"app_id": "a", "app_secret": "b", "alert_chats": ["oc_default"], "report_chats": ["oc_rep"]},
                           host, api=api, receiver=FakeReceiver())
        await ch.start()
        host.bus.publish(TOPIC_ALERT, {"kind": "alert", "condition": "禁止吸烟", "targets": ["feishu:oc_x", "web"], "ts": time.time()})
        host.bus.publish(TOPIC_ALERT, {"kind": "alert", "condition": "离岗", "targets": ["dingtalk:cid1"], "ts": time.time()})
        host.bus.publish(TOPIC_ALERT, {"kind": "alert", "condition": "明火", "targets": ["default"], "ts": time.time()})
        host.bus.publish(TOPIC_ALERT, {"kind": "report", "title": "日报", "text": "无事", "targets": ["feishu"]})
        await asyncio.sleep(0.05)
        assert [s[0] for s in api.sent] == ["oc_x", "oc_default", "oc_rep"]
        await ch.stop()
    asyncio.run(run())


def test_app_wires_duties(tmp_path):
    async def run():
        from tests.test_channels import _app
        app = _app(tmp_path, FakeLLM([]), vlm=FakeVLM(answers={"要复核的情况": YES, "放大图": YES}))
        app.cfg["duties"]["verify_delay"] = 0
        app.duties.verifier.delay = 0
        d = app.duties.book.create(name="禁止吸烟", condition="有人吸烟", targets=["web"])
        ui = app.bus.subscribe(TOPIC_UI)
        await app.start()
        assert "禁止吸烟" in app.agent._system()["content"] and app.station.station.name in app.agent._system()["content"]
        app.duties.engine.on_hit(d.id, "疑似吸烟", time.time(), b"\xff\xd8x")
        await asyncio.sleep(0.1)
        types = [e["type"] for e in _drain(ui)]
        assert "alert_new" in types and "alert" in types
        assert app.status()["stats"]["pending"] == 1
        assert app.target_problem("dingtalk:cid") == "钉钉未开启"
        await app.stop()
    asyncio.run(run())


def test_duty_web_api(tmp_path):
    import pytest
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tests.test_channels import _app
    from vva.web.duties_api import register_duty_routes

    app = _app(tmp_path, FakeLLM([]), vlm=FakeVLM(answers={"要复核的情况": YES, "放大图": YES}))
    app.duties.verifier.delay = 0
    api = FastAPI()
    register_duty_routes(api, app, "pw", os.path.join(ROOT, "vva", "web", "static"))
    c = TestClient(api)
    h = {"X-Token": "pw"}
    assert c.post("/api/duties", json={"condition": "有人吸烟"}).status_code == 401
    assert c.post("/api/duties", headers=h, json={"name": "巡检", "kind": "inspect", "condition": "读表"}).status_code == 400
    d = c.post("/api/duties", headers=h, json={"name": "禁止吸烟", "condition": "有人吸烟", "targets": ["web"],
                                               "level": "urgent", "weekdays": ["1", 9]}).json()
    assert d["id"] == "d1" and d["weekdays"] == [1]
    assert c.put("/api/duties/d1", headers=h, json={"enabled": False}).json()["enabled"] is False
    ov = c.get("/api/overview").json()
    assert ov["duties"][0]["state"] == "paused" and ov["station"]["name"]
    t = c.post("/api/duties/test", json={"duty": {"condition": "有人吸烟", "name": "试"}}).json()
    assert t["match"] and t["image"].startswith("data:image/jpeg") and t["hints"]
    created = c.post("/api/duties/templates/warehouse", headers=h, json={"names": ["明火烟雾", "每日值守报告"],
                                                                       "targets": ["web"], "mode": "trial"}).json()["created"]
    assert [x["name"] for x in created] == ["明火烟雾", "每日值守报告"] and created[0]["mode"] == "trial"
    duty = app.duties.book.get("d1")
    a = app.duties.alerts.create(duty, "有人吸烟", [("初次发现", b"\xff\xd8x")])
    lst = c.get("/api/alerts").json()
    assert lst["alerts"][0]["id"] == a.id and lst["stats"]["pending"] == 1
    r = c.post(f"/api/alerts/{a.id}/action", json={"action": "false", "actor": "张三", "add_ref": True}).json()
    assert r["status"] == "false" and r["ref_added"] and app.duties.book.get("d1").negatives
    assert c.get(f"/api/alerts/image/{r['images'][0]['file']}").status_code == 200
    assert c.get("/api/alerts/image/../../etc/passwd").status_code == 404
    assert c.delete(f"/api/alerts/{a.id}").status_code == 401
    assert c.delete(f"/api/alerts/{a.id}", headers=h).json()["deleted"] == 1
    assert c.post("/api/alerts/delete", headers=h, json={"ids": []}).status_code == 400
    assert c.put("/api/station", headers=h, json={"name": "1 号仓库门口", "paused": True}).json()["paused"] is True
    assert app.rules.paused and c.delete("/api/duties/d1", headers=h).json()["ok"]
    assert c.get("/duties").status_code == 200 and c.get("/alerts").status_code == 200


def test_duty_changes_refresh_agent_context(tmp_path):
    from tests.test_channels import _app
    app = _app(tmp_path, FakeLLM([]))
    ids = [app.duties.book.create(name=f"职责{i}", condition=f"条件{i}").id for i in range(6)]
    conv = app.conversations.get("web")
    conv.extend([{"role": "user", "content": "你负责什么"}, {"role": "assistant", "content": "我负责 6 项。"}])
    app.duties.book.remove(ids[0])
    app.duties.book.remove(ids[1])
    notes = [m["content"] for m in conv.messages if m["content"].startswith(app.DUTY_NOTE)]
    assert notes == [f"{app.DUTY_NOTE}现在长期职责 4 项：职责2、职责3、职责4、职责5；没有临时任务。"]   # 只保留最新一条
    system = app.agent._system()["content"]
    assert "长期职责 4 项" in system and "职责0" not in system and "以这里为准" in system


def test_observer_matches_rule_by_name_and_recent_chats_persist(tmp_path):
    from tests.test_channels import FakeAPI, FakeHost, FakeReceiver
    from vva.channels.dingtalk.channel import DingTalkChannel
    from vva.perception.observer import Observer
    from vva.perception.timeline import Timeline
    m = make_duties()
    d = m.book.create(name="玩手机", condition="画面中有人低头玩手机")
    obs = Observer(FrameBuffer(), FakeVLM(), Timeline(), m.book, EventBus())
    assert obs._match_rule(d.id) is d and obs._match_rule("玩手机") is d and obs._match_rule("“画面中有人低头玩手机”") is d
    assert obs._match_rule("没有这个") is None
    path = str(tmp_path / "dingtalk.recent.json")
    ch = DingTalkChannel({"client_id": "a", "client_secret": "b", "_recent_file": path}, FakeHost(), api=FakeAPI(), receiver=FakeReceiver())
    ch._remember_chat(__import__("vva.channels.dingtalk.message", fromlist=["parse_message"]).parse_message(
        {"msgId": "m1", "conversationId": "cidG", "conversationType": "2", "senderStaffId": "u1", "msgtype": "text",
         "text": {"content": "hi"}, "conversationTitle": "安全群"}))
    ch2 = DingTalkChannel({"client_id": "a", "client_secret": "b", "_recent_file": path}, FakeHost(), api=FakeAPI(), receiver=FakeReceiver())
    assert list(ch2.recent) == ["cidG"] and ch2.recent["cidG"]["title"] == "安全群"


def test_duty_card_action_and_period_text():
    from vva.alerts.service import day_start
    m = make_duties()
    m.target_options = lambda: [{"id": "web", "label": "值守台"}, {"id": "voice", "label": "现场播报"},
                                {"id": "dingtalk:cidA", "label": "钉钉·安全群"}]
    m.book.create(name="禁止吸烟", condition="有人吸烟", targets=["web", "dingtalk:cidA", "voice"],
                  weekdays=[1, 2, 3, 4, 5], hours="08:00-18:00")
    m.book.create(name="快递提醒", condition="快递到门口", targets=["web"], temporary=True,
                  expires=day_start() + 86400 + 18 * 3600)
    a, b = m.status_list()
    assert a["action_text"] == "发钉钉·安全群、现场喇叭播报" and a["period_text"] == "工作日 08:00-18:00"
    assert b["action_text"] == "未设置推送" and b["period_text"] == "截止到明天 18:00"
    assert "处置：发钉钉·安全群" in m.prompt_block()


# ---------------------------------------------------------------- 执行（带编号的动作）
def make_actions(**kw):
    from vva.actions import ActionService
    from vva.core.bus import EventBus
    from vva.core.sqlite import SqliteDB
    said, calls = [], []

    async def notify(title, text):
        return ["钉钉告警群"]

    def http(url, method, payload, headers, timeout):
        calls.append((url, method, payload))
        return "设备已响应（200）"
    import tempfile
    s = ActionService(SqliteDB(":memory:"), EventBus(), shots_dir=tempfile.mkdtemp(), speak=said.append, notify=notify,
                      snapshot=lambda: b"jpeg", station_name=lambda: "一号仓", http=http, **kw)
    return s, said, calls


def test_actions_seeded_with_codes_and_readiness():
    s, _, _ = make_actions()
    items = {a["code"]: a for a in s.list()}
    assert items[1]["name"] == "现场喇叭喊话" and items[1]["ready"]
    assert items[8]["danger"] and not items[8]["ready"] and "未接入" in items[8]["problem"]
    assert s.find("3号").code == 3 and s.find("03").code == 3 and s.find("抓拍").code == 3
    assert "3 号 抓拍存证" in s.prompt_block()


def test_action_run_speaker_snapshot_and_log():
    s, said, _ = make_actions()
    r = asyncio.run(s.run(1, text="请离开"))
    assert r["ok"] and said == ["请离开"]
    r = asyncio.run(s.run("3"))
    assert r["ok"] and s.shot_path(s.store.runs(1)[0]["image"])
    r = asyncio.run(s.run(4))
    assert not r["ok"] and "未接入" in r["error"]
    assert [x["ok"] for x in s.store.runs(3)] == [False, True, True]
    assert not asyncio.run(s.run(99))["ok"]


def test_action_webhook_and_deleted_seed_stays_deleted():
    s, _, calls = make_actions()
    s.update(4, {"config": {"url": "http://gw.local/alarm", "body": {"relay": 2}}})
    r = asyncio.run(s.run(4))
    assert r["ok"] and calls[0][0] == "http://gw.local/alarm" and calls[0][2]["relay"] == 2 and calls[0][2]["code"] == 4
    s.store.remove(6)
    from vva.actions import ActionStore
    assert ActionStore(s.store.db).get(6) is None
    assert s.create({"name": "加速"}).code == 6


def test_danger_action_needs_confirm_in_a_later_turn():
    from vva.core import turn
    s, _, calls = make_actions()
    s.update(8, {"config": {"url": "http://gw.local/power"}})

    async def call(**kw):
        turn.begin("web")
        return await s.tools.tool_run_action(**kw)
    r = asyncio.run(call(code="8", confirm=True))          # 模型一上来就带 confirm 也不放行
    assert r.get("need_confirm") and not calls

    async def same_turn():
        turn.begin("web")
        await s.tools.tool_run_action(code="8")
        return await s.tools.tool_run_action(code="8", confirm=True)   # 同一轮里自问自答不算
    assert asyncio.run(same_turn()).get("need_confirm") and not calls
    r = asyncio.run(call(code="8", confirm=True))          # 用户下一句确认
    assert r["ok"] and len(calls) == 1
    r = asyncio.run(call(code="1"))                         # 普通动作直接执行
    assert r["ok"]
