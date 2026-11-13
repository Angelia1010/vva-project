"""核心逻辑测试，不需要摄像头、麦克风和真实模型。运行：python -m pytest -q tests"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.fakes import FakeASR, FakeLLM, FakeTTS, FakeVLM, silence_pcm, speech_pcm  # noqa: E402
from vva.agent.agent import Agent  # noqa: E402
from vva.agent.tools import VisionTools  # noqa: E402
from vva.audio.push import CallbackAudioOutput, PushAudioInput  # noqa: E402
from vva.audio.vad import VADSegmenter  # noqa: E402
from vva.core.bus import TOPIC_ALERT, TOPIC_UI, EventBus  # noqa: E402
from vva.core.config import load_config  # noqa: E402
from vva.core.jsonutil import parse_json  # noqa: E402
from vva.core.plugin import load_class  # noqa: E402
from vva.core.types import Frame  # noqa: E402
from vva.perception.imaging import box_to_normalized, crop_region, validate_region  # noqa: E402
from vva.perception.observer import Observer  # noqa: E402
from vva.perception.timeline import Timeline, WatchRules  # noqa: E402
from vva.video.frame_buffer import FrameBuffer  # noqa: E402
from vva.voice.session import VoiceSession  # noqa: E402
from vva.voice.text_split import SentenceSplitter, ThinkFilter  # noqa: E402


def _img(v: int = 100, w: int = 640, h: int = 360) -> np.ndarray:
    return np.full((h, w, 3), v, np.uint8)


def _drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ---------------------------------------------------------------- 配置与工具函数
def test_config_defaults_and_env(tmp_path):
    os.environ["TEST_VLM_KEY"] = "sk-test"
    p = tmp_path / "c.yaml"
    p.write_text("models:\n  vlm:\n    base_url: http://x/v1\n    api_key: ${TEST_VLM_KEY}\n    model: vl\n"
                 "  asr:\n    model: asr1\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg["models"]["vlm"]["api_key"] == "sk-test"
    assert cfg["models"]["asr"]["base_url"] == "http://x/v1"      # 沿用 vlm
    assert cfg["models"]["agent_llm"]["model"] == "vl"            # 沿用 vlm
    assert cfg["video"]["buffer"]["seconds"] == 60                # 默认值


def test_parse_json_variants():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('<think>xx</think>结果：{"a": 2} 完毕') == {"a": 2}
    assert parse_json("没有 json") == {}


def test_regions_and_boxes():
    assert validate_region([0.1, 0.2, 0.5, 0.6]) == [0.1, 0.2, 0.5, 0.6]
    assert validate_region([100, 200, 500, 600]) == [0.1, 0.2, 0.5, 0.6]   # 误传 0~1000 自动换算
    assert validate_region([0.5, 0.5, 0.5, 0.5]) is None
    assert box_to_normalized([64, 36, 320, 180], "pixel", 640, 360) == [0.1, 0.1, 0.5, 0.5]
    assert box_to_normalized([100, 100, 500, 500], "0-1000", 640, 360) == [0.1, 0.1, 0.5, 0.5]
    crop = crop_region(_img(), [0.4, 0.4, 0.5, 0.5], min_side=500)
    assert max(crop.shape[:2]) == 500


def test_plugin_loader():
    cls = load_class(os.path.join(ROOT, "examples", "custom_video_source.py") + ":TestPatternSource")
    src = cls(fps=20)
    got = []
    src.on_frame(got.append)
    src.start()
    time.sleep(0.3)
    src.stop()
    assert got and got[0].image.shape == (720, 1280, 3)


# ---------------------------------------------------------------- 帧缓存
def test_frame_buffer():
    buf = FrameBuffer(seconds=10, fps=2)
    now = time.time()
    for i in range(20):  # 模拟过去 5 秒、每 0.25 秒一帧
        buf.add(Frame(image=_img(i * 10), ts=now - 5 + i * 0.25))
    assert 9 <= buf.stats()["buffered_frames"] <= 11   # 按 2 帧/秒抽样
    seq = buf.range(5, 4)
    assert len(seq) == 4 and seq[0].ts < seq[-1].ts
    assert abs(buf.at(3).ts - (now - 3)) <= 0.5
    assert buf.latest_snapshot() is not None
    assert buf.preview_jpeg()[1][:2] == b"\xff\xd8"


# ---------------------------------------------------------------- 文本切分
def test_think_filter_and_splitter():
    f = ThinkFilter()
    out = "".join(f.feed(x) for x in ["<thi", "nk>内部推理</th", "ink>表上读数", "是 42。"]) + f.flush()
    assert out == "表上读数是 42。"
    s = SentenceSplitter()
    sents = []
    for ch in "好的，我看一下。表上读数是**42**兆帕！还有别的吗":
        sents += s.feed(ch)
    sents += s.flush()
    assert "表上读数是42兆帕！" in sents and sents[-1] == "还有别的吗"


# ---------------------------------------------------------------- VAD
def test_vad_segments_speech():
    vad = VADSegmenter(sample_rate=16000, engine="energy")
    events = []
    for chunk in (silence_pcm(1.0), speech_pcm(1.0), silence_pcm(1.2)):
        for i in range(0, len(chunk), 960):  # 30ms 一包
            events += vad.feed(chunk[i:i + 960])
    kinds = [e[0] for e in events]
    assert kinds == ["start", "end"]
    dur = len(events[1][1]) / 2 / 16000
    assert 1.0 <= dur <= 2.2


# ---------------------------------------------------------------- 智能体 + 工具
def _tools(vlm, bus=None):
    buf = FrameBuffer(seconds=30, fps=2)
    buf.add(Frame(image=_img(), ts=time.time()))
    bus = bus or EventBus()
    return VisionTools(buf, vlm, Timeline(), WatchRules(), bus), bus


def test_agent_tool_loop():
    async def run():
        vlm = FakeVLM(answers={"找出": '{"found": true, "objects": [{"label": "表盘", "box_2d": [300, 200, 700, 800]}]}'})
        tools, bus = _tools(vlm)
        ui = bus.subscribe(TOPIC_UI)
        llm = FakeLLM([
            [("locate", {"target": "表盘"})],
            [("look_now", {"question": "读数是多少", "region": [0.3, 0.2, 0.7, 0.8]})],
            "表上读数大约是 42。",
        ])
        agent = Agent(llm, tools, filler_text="我看一下。")
        said = []
        final = await agent.run("表上读数是多少", said.append)
        assert final == "表上读数大约是 42。"
        assert said[0] == "我看一下。"                       # 配置了 filler_text 时，调工具前先说一句
        tool_msgs = [m for m in agent.history if m["role"] == "tool"]
        assert len(tool_msgs) == 2
        assert json.loads(tool_msgs[0]["content"])["objects"][0]["box"] == [0.3, 0.2, 0.7, 0.8]
        events = [e["type"] for e in _drain(ui)]
        assert "bbox" in events and events.count("tool") == 2
        # 第二轮请求中应带上第一轮的工具结果
        assert any(m.get("role") == "tool" for m in llm.calls[1])
    asyncio.run(run())


def test_watch_tools_and_history_trim():
    async def run():
        tools, _ = _tools(FakeVLM())
        r = json.loads(await tools.call("add_watch", {"condition": "画面中没有人"}))
        assert r["ok"] and r["rule"]["id"] == "r1"
        assert json.loads(await tools.call("list_watches", {}))["rules"][0]["condition"] == "画面中没有人"
        assert json.loads(await tools.call("remove_watch", {"rule_id": "r1"}))["ok"]
        assert "error" in json.loads(await tools.call("no_such_tool", {}))
        agent = Agent(FakeLLM(["好"] * 20), tools, max_history_turns=3)
        for i in range(6):
            await agent.run(f"问题{i}", lambda _: None)
        assert sum(m["role"] == "user" for m in agent.history) == 3
    asyncio.run(run())


# ---------------------------------------------------------------- 后台观察
def test_observer_timeline_and_alert():
    async def run():
        bus = EventBus()
        alerts, ui = bus.subscribe(TOPIC_ALERT), bus.subscribe(TOPIC_UI)
        buf = FrameBuffer()
        rules = WatchRules()
        rule = rules.add("画面中没有人", cooldown_s=60)
        vlm = FakeVLM(default=json.dumps({
            "summary": "办公室里空无一人", "people_count": 0, "objects": ["桌子"], "activities": [],
            "anomalies": [], "triggered_rules": [{"id": rule.id, "evidence": "座位空着"}]}, ensure_ascii=False))
        obs = Observer(buf, vlm, Timeline(), rules, bus)
        buf.add(Frame(image=_img(50)))
        assert (await obs.step()).people_count == 0
        assert "画面中没有人" in vlm.requests[0][1]            # 规则写进了提示词
        a = _drain(alerts)
        assert len(a) == 1 and a[0]["evidence"] == "座位空着"
        assert any(e["type"] == "timeline" for e in _drain(ui))
        # 画面不变 → 跳过；画面变化 → 再看一次，但冷却期内不重复提醒
        buf.add(Frame(image=_img(50)))
        assert await obs.step() is None
        buf.add(Frame(image=_img(200)))
        assert await obs.step() is not None
        assert _drain(alerts) == []
    asyncio.run(run())


# ---------------------------------------------------------------- 语音会话
def test_voice_session_end_to_end():
    async def run():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        mic, spk = PushAudioInput(16000), CallbackAudioOutput(24000)
        played, cleared = [], []
        spk.add_sink(played.append)
        spk.add_clear_listener(lambda: cleared.append(1))
        asr, tts = FakeASR("表上读数是多少"), FakeTTS()
        heard = []

        async def on_utt(text):
            heard.append(text)

        vs = VoiceSession(mic, spk, asr, tts, VADSegmenter(16000, engine="energy"), bus,
                          on_utterance=on_utt, mute_mic_while_speaking=False,
                          mode="wake_word", wake_words=["小智"])
        await vs.start()

        async def speak_into_mic(pcm):
            for i in range(0, len(pcm), 960):
                mic.push_pcm(pcm[i:i + 960])
                await asyncio.sleep(0)

        # 1) 未说唤醒词 → 忽略
        await speak_into_mic(silence_pcm(0.5) + speech_pcm(0.8) + silence_pcm(1.0))
        await asyncio.sleep(0.2)
        assert heard == []
        # 2) 带唤醒词 → 去掉唤醒词后交给上层
        asr.text = "小智，表上读数是多少"
        await speak_into_mic(speech_pcm(0.8) + silence_pcm(1.0))
        await asyncio.sleep(0.2)
        assert heard == ["表上读数是多少"]
        # 3) 播放 → 用户开口 → 打断
        vs.say("表上读数是四十二。")
        await asyncio.sleep(0.1)
        assert played and tts.texts == ["表上读数是四十二。"]
        assert vs.speaking
        await speak_into_mic(speech_pcm(0.3))
        await asyncio.sleep(0.05)
        assert cleared, "用户说话时应清空播放"
        await vs.stop()
    asyncio.run(run())


# ---------------------------------------------------------------- 整体编排
def test_app_integration(tmp_path):
    async def run():
        from vva.app import App
        cfg = load_config(None)
        cfg["video"].update({"type": "custom", "custom": {
            "class": os.path.join(ROOT, "examples", "custom_video_source.py") + ":TestPatternSource",
            "params": {"fps": 20}}})
        cfg["audio"]["input"]["type"] = "push"
        cfg["audio"]["output"]["type"] = "push"
        cfg["observer"]["interval"] = 0.3
        cfg["memory"]["dir"] = str(tmp_path / "memory")
        obs_json = json.dumps({"summary": "一块表盘", "people_count": 0, "objects": ["表盘"],
                               "activities": [], "anomalies": [], "triggered_rules": []}, ensure_ascii=False)
        vlm = FakeVLM(answers={"监控画面": obs_json})
        llm = FakeLLM([[("look_now", {"question": "读数"})], "读数是四十二。"])
        tts = FakeTTS()
        app = App(cfg, llm=llm, vlm=vlm, asr=FakeASR(), tts=tts)
        ui = app.bus.subscribe(TOPIC_UI)
        await app.start()
        await asyncio.sleep(0.8)                      # 等画面和后台观察
        assert app.buffer.latest_frame() is not None
        assert len(app.timeline) >= 1
        await app.handle_user_text("表上读数是多少")
        await app._turn
        await asyncio.sleep(0.2)
        types = [e["type"] for e in _drain(ui)]
        for t in ("user", "assistant_start", "tool", "assistant_delta", "assistant_end", "timeline", "tts"):
            assert t in types, t
        assert tts.texts == ["读数是四十二。"]               # 默认不播报“我看一下”之类的过渡语
        # 新问题会打断旧回答
        app.agent.llm = FakeLLM([[("look_sequence", {"seconds": 5, "question": "x"})], "好"])
        await app.handle_user_text("问题一")
        await app.handle_user_text("问题二")
        await app._turn
        users = [m["content"] for m in app.agent.history if m["role"] == "user"]
        assert users[-1] == "问题二"
        await app.stop()
    asyncio.run(run())
