"""多会话、场景包、飞书入口的测试（飞书用假的 API 和事件，不需要 lark-oapi 和网络）。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.fakes import FakeLLM, FakeVLM  # noqa: E402
from vva.agent.agent import Agent  # noqa: E402
from vva.agent.conversation import ConversationStore  # noqa: E402
from vva.agent.toolkit import ToolSet  # noqa: E402
from vva.agent.tools import VisionTools  # noqa: E402
from vva.app import WEB_SESSION, App, Reply  # noqa: E402
from vva.channels import build_channels  # noqa: E402
from vva.channels.feishu import cards  # noqa: E402
from vva.channels.feishu.api import FeishuAPI, FeishuError  # noqa: E402
from vva.channels.feishu.channel import FeishuChannel  # noqa: E402
from vva.channels.feishu.message import parse_event  # noqa: E402
from vva.core import turn  # noqa: E402
from vva.core.bus import TOPIC_ALERT, TOPIC_UI, EventBus  # noqa: E402
from vva.core.config import load_config  # noqa: E402
from vva.core.types import Frame  # noqa: E402
from vva.perception.imaging import encode_jpeg  # noqa: E402
from vva.perception.observer import Observer  # noqa: E402
from vva.perception.timeline import Timeline, WatchRules  # noqa: E402
from vva.scene import SceneManager  # noqa: E402
from vva.video.frame_buffer import FrameBuffer  # noqa: E402


def _img(v=100):
    return np.full((360, 640, 3), v, np.uint8)


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ---------------------------------------------------------------- 多会话
def test_conversations_are_isolated_and_readonly_hides_write_tools():
    async def run():
        buf = FrameBuffer()
        buf.add(Frame(image=_img(), ts=time.time()))
        tools = VisionTools(buf, FakeVLM(), Timeline(), WatchRules(), EventBus())
        llm = FakeLLM([[("look_now", {"question": "几个人"})], "两人。", [("add_watch", {"condition": "x"})], "好。"])
        agent = Agent(llm, tools)
        store = ConversationStore(max_turns=8)
        a, b = store.get("feishu:a"), store.get("feishu:b", readonly=True)
        await agent.run("几个人", lambda _: None, conversation=a)
        assert [m["role"] for m in a.messages][0] == "user" and b.messages == []
        await agent.run("有人就提醒我", lambda _: None, conversation=b)
        names = {s["function"]["name"] for s in agent._schemas(b)}
        assert "add_watch" not in names and "look_now" in names
        tool_msg = next(m for m in b.messages if m["role"] == "tool")
        assert "没有修改权限" in tool_msg["content"]            # 模型硬调也会被拦下
        assert tools.rules.list() == []
    asyncio.run(run())


def test_bus_tags_ui_events_with_current_session():
    async def run():
        bus = EventBus()
        q = bus.subscribe(TOPIC_UI)

        async def in_turn():
            turn.begin("feishu:x")
            bus.publish(TOPIC_UI, {"type": "tool"})
            turn.attach(b"\xff\xd8jpeg", "画面")
        await asyncio.create_task(in_turn())
        bus.publish(TOPIC_UI, {"type": "timeline"})
        ev = _drain(q)
        assert ev[0]["session"] == "feishu:x" and "session" not in ev[1]
    asyncio.run(run())


def _app(tmp_path, llm, vlm=None, **cfg_over):
    cfg = load_config(None)
    cfg["video"]["type"] = "push"
    cfg["observer"]["enabled"] = False
    cfg["memory"]["dir"] = str(tmp_path / "memory")
    cfg["scene"]["dir"] = str(tmp_path / "scene")
    for k, v in cfg_over.items():
        cfg[k].update(v)
    app = App(cfg, llm=llm, vlm=vlm or FakeVLM(default="依据：x\n结论：画面中有 2 人\n把握：高"), enable_audio=False)
    app.buffer.add(Frame(image=_img(), ts=time.time()))
    return app


def test_app_ask_returns_answer_with_attachments(tmp_path):
    async def run():
        llm = FakeLLM([[("look_now", {"question": "几个人"})], "画面中有 2 人。", "好的。"])
        app = _app(tmp_path, llm)
        ui = app.bus.subscribe(TOPIC_UI)
        await app.start()
        reply = await app.ask("几个人", "feishu:g1", source="feishu")
        assert isinstance(reply, Reply) and reply.text == "画面中有 2 人。"
        assert len(reply.attachments) == 1 and reply.attachments[0].jpeg[:2] == b"\xff\xd8"
        assert app.conversations.peek(WEB_SESSION) is None                     # 没有写进网页会话
        sessions = {e.get("session") for e in _drain(ui) if e["type"].startswith("assistant")}
        assert sessions == {"feishu:g1"}
        app.add_note("feishu:g1", "提醒：有人离开")
        assert app.conversations.get("feishu:g1").messages[-1]["content"] == "提醒：有人离开"
        await app.stop()
    asyncio.run(run())


# ---------------------------------------------------------------- 场景包
def _jpeg(v=120):
    return encode_jpeg(_img(v))


def test_scene_store_and_context(tmp_path):
    sm = SceneManager({"dir": str(tmp_path / "s"), "max_ref_images": 2})
    store = sm.store
    store.save_scene(description="设备间", areas={"右上方": "压力表"}, hotwords=["兆帕"],
                     standards=["压力 0.2~0.5 兆帕为正常"], rules=[{"condition": "有人进入画面", "cooldown_s": 300}])
    store.save_object("压力表", aliases=["表盘"], description="白色圆表", normal="0.2~0.5", abnormal="超过 0.6")
    for v in (10, 20, 30):
        store.add_image("压力表", _jpeg(v))
    assert store.scene.objects["压力表"].images == ["ref_01.jpg", "ref_02.jpg", "ref_03.jpg"]
    # 重新加载后内容一致
    sm2 = SceneManager({"dir": str(tmp_path / "s")})
    assert sm2.store.scene.standards == ["压力 0.2~0.5 兆帕为正常"] and "压力表" in sm2.store.scene.objects
    block = sm.ctx.prompt_block()
    assert "设备间" in block and "右上方是压力表" in block and "【压力表】（也叫表盘）" in block
    assert sm.ctx.find("表盘").name == "压力表" and sm.ctx.match("看看表盘读数")[0].name == "压力表"
    assert len(sm.ctx.references(store.scene.objects["压力表"])) == 2           # max_ref_images
    assert sm.ctx.asr_prompt() == "兆帕、压力表、表盘"
    rules = WatchRules()
    assert sm.apply_rules(rules) == 1 and sm.apply_rules(rules) == 0           # 不重复添加
    assert store.delete_image("压力表", "ref_02.jpg") and not store.delete_image("压力表", "../x.jpg")
    assert store.delete_object("压力表") and sm.ctx.prompt_block().count("【") == 0


def test_compare_with_reference_tool(tmp_path):
    async def run():
        sm = SceneManager({"dir": str(tmp_path / "s")})
        buf = FrameBuffer()
        buf.add(Frame(image=_img(), ts=time.time()))
        vlm = FakeVLM(default="依据：指针在 0.3\n结论：读数约 0.3 兆帕，正常\n把握：中")
        tools = sm.tools(buf, vlm, EventBus())
        assert tools.schemas() == []                                       # 没有目标时不提供工具
        sm.store.save_object("压力表", description="白色圆表")
        sm.store.add_image("压力表", _jpeg())
        assert tools.schemas()[0]["function"]["name"] == "compare_with_reference"
        ts = ToolSet(tools)                                                  # 运行中新增目标后也能调用
        r = json.loads(await ts.call("compare_with_reference", {"target": "压力表", "question": "读数正常吗"}))
        assert r["answer"].startswith("读数约 0.3") and r["reference_images"] == 1
        n_images, prompt = vlm.requests[-1]
        assert n_images == 2 and "前 1 张图是“压力表”的参考图" in prompt
        r = json.loads(await ts.call("compare_with_reference", {"target": "叉车", "question": "x"}))
        assert "没有“叉车”" in r["error"]
    asyncio.run(run())


def test_observer_uses_reference_images_for_rules(tmp_path):
    async def run():
        sm = SceneManager({"dir": str(tmp_path / "s")})
        sm.store.save_object("访客工牌", description="红色挂绳")
        sm.store.add_image("访客工牌", _jpeg())
        sm.store.save_scene(description="前台")
        buf = FrameBuffer()
        buf.add(Frame(image=_img()))
        rules = WatchRules()
        rules.add("出现佩戴访客工牌的人")
        vlm = FakeVLM(default=json.dumps({"summary": "前台有人", "people_count": 1}, ensure_ascii=False))
        obs = Observer(buf, vlm, Timeline(), rules, EventBus(), hint_provider=sm.ctx.observer_hint,
                       references=sm.ctx.references_for_text)
        await obs.step()
        n, prompt = vlm.requests[0]
        assert n == 2 and "图1是“访客工牌”的参考图1" in prompt and "场景说明：前台" in prompt
    asyncio.run(run())


def test_app_scene_wiring(tmp_path):
    async def run():
        llm = FakeLLM(["好。"])
        app = _app(tmp_path, llm)
        app.scene.store.save_object("叉车充电桩", description="绿色柜体")
        app.scene.changed()
        await app.handle_user_text("看看")
        await app._turn
        system = llm.calls[0][0]["content"]
        assert "## 场景包" in system and "叉车充电桩" in system
        app.scene.store.save_scene(rules=[{"condition": "充电桩亮红灯"}])
        app.scene.changed()
        assert [r.condition for r in app.rules.list()] == ["充电桩亮红灯"]      # 网页上新增规则模板立即生效
        await app.stop()
    asyncio.run(run())


# ---------------------------------------------------------------- 飞书
def _event(text="画面里有几个人", chat_type="p2p", mid="m1", chat="oc_1", user="ou_1", mtype="text",
           content=None, mentions=None):
    return {"schema": "2.0", "header": {"event_type": "im.message.receive_v1"}, "event": {
        "sender": {"sender_id": {"open_id": user}},
        "message": {"message_id": mid, "chat_id": chat, "chat_type": chat_type, "message_type": mtype,
                    "content": json.dumps(content if content is not None else {"text": text}, ensure_ascii=False),
                    "mentions": mentions}}}


def test_parse_event_variants():
    m = parse_event(_event("@_user_1 画面里有几个人", chat_type="group", mentions=[{"key": "@_user_1"}]))
    assert m.text == "画面里有几个人" and m.is_group and m.mentioned
    post = {"zh_cn": {"title": "", "content": [[{"tag": "at", "user_id": "x"}, {"tag": "text", "text": "门口有人吗"}]]}}
    assert parse_event(_event(mtype="post", content=post)).text == "门口有人吗"
    a = parse_event(_event(mtype="audio", content={"file_key": "fk", "duration": 2000}))
    assert a.msg_type == "audio" and a.file_key == "fk"
    assert parse_event({"event": {}}) is None


class FakeAPI:
    def __init__(self):
        self.sent, self.replies, self.uploads = [], [], 0

    async def send(self, chat, msg_type, content, receive_id_type="chat_id"):
        self.sent.append((chat, msg_type, content))

    async def reply(self, mid, msg_type, content):
        self.replies.append((mid, msg_type, content))

    async def upload_image(self, jpeg):
        self.uploads += 1
        return f"img_{self.uploads}"

    async def download_resource(self, mid, key, kind="file"):
        return b"opus-bytes"

    async def close(self):
        pass


class FakeReceiver:
    started = False

    def start(self):
        FakeReceiver.started = True


class FakeHost:
    def __init__(self):
        self.bus = EventBus()
        self.asked, self.notes, self.resets = [], [], []

    async def ask(self, text, session, source="api", readonly=False):
        self.asked.append((text, session, readonly))
        return Reply("画面中有 2 人。", [turn.Attachment(b"\xff\xd8x", "已定位并放大：门口")])

    def add_note(self, session, text):
        self.notes.append((session, text))

    def reset_session(self, session):
        self.resets.append(session)

    def snapshot(self, max_side=1280):
        return b"\xff\xd8snap"

    async def transcribe(self, pcm, sr):
        return "门口有人吗"

    async def report(self, start, end):
        return "今天共 2 次提醒。"


def _channel(**cfg):
    host, api = FakeHost(), FakeAPI()
    ch = FeishuChannel({"app_id": "a", "app_secret": "b", **cfg}, host, api=api, receiver=FakeReceiver())
    return ch, host, api


def test_feishu_question_and_commands():
    async def run():
        ch, host, api = _channel()
        await ch.handle_event(_event("画面里有几个人"))
        assert host.asked == [("画面里有几个人", "feishu:oc_1", False)]
        mid, mtype, card = api.replies[-1]
        assert mid == "m1" and mtype == "interactive"
        assert card["elements"][0]["text"]["content"] == "画面中有 2 人。" and card["elements"][1]["img_key"] == "img_1"
        await ch.handle_event(_event("画面里有几个人"))                     # 同一条消息重复推送：忽略
        assert len(host.asked) == 1
        await ch.handle_event(_event("/id", mid="m2"))
        assert "oc_1" in api.replies[-1][2]["text"] and "ou_1" in api.replies[-1][2]["text"]
        await ch.handle_event(_event("清空对话", mid="m3"))
        assert host.resets == ["feishu:oc_1"]
        await ch.handle_event(_event("几个人", chat_type="group", mid="m4"))   # 群里没有 @：忽略
        assert len(host.asked) == 1
    asyncio.run(run())


def test_feishu_permissions():
    async def run():
        ch, host, api = _channel(allowed_chats=["oc_ok"], admin_users=["ou_admin"])
        await ch.handle_event(_event("几个人", chat="oc_other"))
        assert host.asked == [] and "没有使用监控助手的权限" in api.replies[-1][2]["text"]
        await ch.handle_event(_event("有人就提醒我", chat="oc_ok", mid="m2", user="ou_guest"))
        await ch.handle_event(_event("有人就提醒我", chat="oc_ok", mid="m3", user="ou_admin"))
        assert [a[2] for a in host.asked] == [True, False]                 # 非管理员为只读会话
    asyncio.run(run())


def test_feishu_voice_message(monkeypatch):
    async def run():
        import vva.channels.feishu.channel as mod

        async def fake_decode(data, sr):
            assert data == b"opus-bytes" and sr == 16000
            return b"\x00\x00" * 1600
        monkeypatch.setattr(mod, "decode_audio_ffmpeg", fake_decode)
        ch, host, api = _channel()
        await ch.handle_event(_event(mtype="audio", content={"file_key": "fk"}))
        assert host.asked[0][0] == "门口有人吗"
    asyncio.run(run())


def test_feishu_alert_and_report():
    async def run():
        ch, host, api = _channel(alert_chats=["oc_alert"], report_chats=["oc_rep"], report_times=["18:00"],
                                 web_url="https://x/browser")
        await ch.start()
        assert FakeReceiver.started
        host.bus.bind_loop(asyncio.get_running_loop())
        host.bus.publish(TOPIC_ALERT, {"rule_id": "r1", "condition": "有人离开座位", "evidence": "工位空着",
                                       "ts": time.time()})
        await asyncio.sleep(0.05)
        chat, mtype, card = api.sent[0]
        assert chat == "oc_alert" and card["header"]["title"]["content"] == "监控提醒"
        assert "有人离开座位" in card["elements"][0]["text"]["content"] and card["elements"][1]["img_key"]
        assert card["elements"][-1]["actions"][0]["url"] == "https://x/browser"
        assert host.notes[0][0] == "feishu:oc_alert"                       # 群里可以接着追问
        # 定时报告：上次报告在 17:00，现在 18:01 → 该发；发完后同一时间点不再发
        t = time.localtime()
        at = lambda h, m: time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1))  # noqa: E731
        ch._last_report = at(17, 0)
        assert ch.due_report(at(18, 1))
        await ch.send_report(at(18, 1))
        assert api.sent[-1][0] == "oc_rep" and api.sent[-1][2]["elements"][0]["text"]["content"] == "今天共 2 次提醒。"
        assert not ch.due_report(at(18, 2))
        await ch.stop()
    asyncio.run(run())


def test_build_channels_from_config():
    host = FakeHost()
    assert build_channels({"feishu": {"enabled": False}}, host) == []
    chans = build_channels({"feishu": {"enabled": True, "app_id": "a", "app_secret": "b"},
                            "unknown": {"enabled": True}}, host)
    assert [c.name for c in chans] == ["feishu"]


class _Resp:
    def __init__(self, data=None, content=b"", status=200, ctype="application/json"):
        self._data, self.content, self.status_code, self.headers = data, content, status, {"content-type": ctype}

    def json(self):
        return self._data


class FakeHTTP:
    def __init__(self):
        self.calls = []

    async def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if url.endswith("tenant_access_token/internal"):
            return _Resp({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        if "/images" in url:
            return _Resp({"code": 0, "data": {"image_key": "img_k"}})
        if "/resources/" in url:
            return _Resp(content=b"audio", ctype="audio/ogg")
        if "/reply" in url:
            return _Resp({"code": 230002, "msg": "bot not in chat"})
        return _Resp({"code": 0, "data": {"message_id": "om_1"}})


def test_feishu_api_requests():
    async def run():
        http = FakeHTTP()
        api = FeishuAPI("a", "b", http=http)
        await api.send("oc_1", "interactive", cards.answer_card("两人"))
        assert await api.upload_image(b"jpg") == "img_k"
        assert await api.download_resource("om_1", "fk") == b"audio"
        token_calls = [c for c in http.calls if c[1].endswith("internal")]
        assert len(token_calls) == 1                                       # 凭证会缓存
        method, url, kw = http.calls[1]
        assert url.endswith("/open-apis/im/v1/messages?receive_id_type=chat_id")
        assert kw["headers"]["Authorization"] == "Bearer t-1"
        assert json.loads(kw["json"]["content"])["header"]["title"]["content"] == "监控助手"
        with pytest.raises(FeishuError):
            await api.reply("om_1", "text", {"text": "x"})
    asyncio.run(run())
