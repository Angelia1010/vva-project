"""监控助手相关功能的测试：防误触发、按住说话、回答限长、角色提示词、长期记忆。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.fakes import (FakeASR, FakeLLM, FakeTTS, FakeVLM, silence_pcm, syllables_pcm,  # noqa: E402
                         typing_pcm, voice_pcm)
from vva.agent.agent import Agent  # noqa: E402
from vva.agent.toolkit import ToolSet  # noqa: E402
from vva.audio.push import CallbackAudioOutput, PushAudioInput  # noqa: E402
from vva.audio.vad import VADSegmenter, speech_band_ratio  # noqa: E402
from vva.core.bus import TOPIC_ALERT, TOPIC_OBSERVATION, TOPIC_UI, EventBus  # noqa: E402
from vva.core.config import load_config  # noqa: E402
from vva.memory import LongTermMemory  # noqa: E402
from vva.memory.store import KIND_ALERT, KIND_COMMAND, KIND_OBSERVATION, MemoryEvent  # noqa: E402
from vva.memory.timeutil import parse_time, window_start  # noqa: E402
from vva.perception.imaging import encode_jpeg  # noqa: E402
from vva.perception.timeline import TimelineEntry, WatchRules  # noqa: E402
from vva.voice.filters import UtteranceFilter  # noqa: E402
from vva.voice.session import VoiceSession  # noqa: E402
from vva.voice.text_split import AnswerLimiter  # noqa: E402


def _vad_events(pcm: bytes, **kw) -> list[str]:
    vad = VADSegmenter(16000, engine="energy", **kw)
    out = []
    for i in range(0, len(pcm), 960):
        out += [e[0] for e in vad.feed(pcm[i:i + 960])]
    return out


def _at(hour: int, minute: int, day_offset: int = 0) -> float:
    d = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return d.timestamp() + day_offset * 86400


def _jpeg(v: int = 120) -> bytes:
    return encode_jpeg(np.full((480, 800, 3), v, np.uint8))


# ---------------------------------------------------------------- 语音防误触发
def test_vad_ignores_typing_but_hears_voice():
    assert speech_band_ratio(voice_pcm(0.03), 16000) > 0.9
    assert _vad_events(silence_pcm(1) + voice_pcm(1.2) + silence_pcm(1.2)) == ["start", "end"]
    # 正常说话字与字之间有弱音，不能要求每帧都连续通过
    assert _vad_events(silence_pcm(1) + syllables_pcm(8) + silence_pcm(1.2)) == ["start", "end"]
    for keys in (8, 14, 20):
        assert _vad_events(silence_pcm(1) + typing_pcm(5, keys_per_s=keys) + silence_pcm(1.2)) == []
    # 对照：旧参数会把敲键盘当成说话
    old = dict(threshold_db=-45, start_frames=3, start_window_ms=90, min_utterance_ms=300, min_voiced_ratio=0,
               min_band_ratio=0)
    assert "end" in _vad_events(silence_pcm(1) + typing_pcm(5, keys_per_s=14, seed=3) + silence_pcm(1.2), **old)


def test_vad_drops_short_blip():
    # 只有 0.3 秒人声：能触发开始，但不够 min_utterance_ms，整句丢弃
    assert _vad_events(silence_pcm(1) + voice_pcm(0.3) + silence_pcm(1.2)) == ["start", "cancel"]


def test_utterance_filter():
    f = UtteranceFilter()
    for text in ("", "。", "嗯", "嗯。", "啊啊", "谢谢观看", "Thank you."):
        assert f.reason_to_drop(text), text
    for text in ("几个人", "表上读数是多少？", "今天上午9点发生了什么"):
        assert f.reason_to_drop(text) is None, text
    assert UtteranceFilter(min_chars=4).reason_to_drop("几个人") == "太短"


def _session(asr, mode="always", **kw):
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    mic, spk = PushAudioInput(16000), CallbackAudioOutput(24000)
    heard: list[str] = []

    async def on_utt(text):
        heard.append(text)

    vs = VoiceSession(mic, spk, asr, FakeTTS(), VADSegmenter(16000, engine="energy"), bus,
                      on_utterance=on_utt, mode=mode, mute_mic_while_speaking=False, ptt_tail_ms=20, **kw)
    return vs, mic, heard, bus


async def _push(mic, pcm):
    for i in range(0, len(pcm), 960):
        mic.push_pcm(pcm[i:i + 960])
        await asyncio.sleep(0)


def test_voice_session_drops_filler_words():
    async def run():
        asr = FakeASR("嗯。")
        vs, mic, heard, bus = _session(asr)
        ui = bus.subscribe(TOPIC_UI)
        await vs.start()
        await _push(mic, voice_pcm(0.8) + silence_pcm(1.0))
        await asyncio.sleep(0.1)
        assert heard == [] and asr.received
        ignored = [e for e in _drain(ui) if e["type"] == "asr" and e.get("ignored")]
        assert ignored and ignored[0]["reason"]
        await vs.stop()
    asyncio.run(run())


def test_push_to_talk():
    async def run():
        asr = FakeASR("画面里有几个人")
        vs, mic, heard, _ = _session(asr, mode="push_to_talk")
        await vs.start()
        # 没按下时说话：不识别
        await _push(mic, voice_pcm(0.8) + silence_pcm(1.0))
        await asyncio.sleep(0.05)
        assert asr.received == []
        # 按下 → 说话 → 松开：识别按下期间的音频（不需要等静音断句）
        vs.ptt_press()
        await _push(mic, voice_pcm(0.8))
        vs.ptt_release()
        await asyncio.sleep(0.1)
        assert heard == ["画面里有几个人"]
        assert abs(asr.received[0] - int(0.8 * 16000) * 2) <= 960 * 2
        # 按一下就松开（太短）：视为误触
        vs.ptt_press()
        await _push(mic, voice_pcm(0.1))
        vs.ptt_release()
        await asyncio.sleep(0.1)
        assert len(asr.received) == 1
        await vs.stop()
    asyncio.run(run())


def test_wake_word_mode_without_words_falls_back():
    async def run():
        vs, *_ = _session(FakeASR(), mode="wake_word")
        assert vs.mode == "always"
    asyncio.run(run())


# ---------------------------------------------------------------- 回答限长与角色
def test_answer_limiter():
    lim = AnswerLimiter(10)
    out = "".join(lim.feed(ch) for ch in "画面中有两个人，都在工位上。左边的人在打电话。右边的人在看屏幕。")
    assert out == "画面中有两个人，都在工位上。"
    lim = AnswerLimiter(10)
    out = lim.feed("一二三四五六七八九十一二三四五六七八九十")
    assert out == "一二三四五六七八九十一二三四五。" and lim.closed and lim.feed("更多") == ""
    assert AnswerLimiter(0).feed("不限制" * 50) == "不限制" * 50


def test_agent_prompt_role_and_notes():
    agent = Agent(FakeLLM([]), ToolSet(), max_answer_chars=60, notes_provider=lambda: ["[n1] 3 号工位是张工的"])
    system = agent._system()["content"]
    assert "监控值守助手" in system and "不在画面里" in system
    assert "不超过30个字" in system
    assert "[n1] 3 号工位是张工的" in system
    assert "星期" in system
    assert "我手里拿的是什么" in system                                  # “我”指代画面中的人时也要看画面


def test_agent_forces_tool_on_first_round():
    async def run():
        llm = FakeLLM([[("list_watches", {})], "当前没有提醒。"])
        from vva.agent.tools import VisionTools
        from vva.perception.timeline import Timeline
        from vva.video.frame_buffer import FrameBuffer
        tools = VisionTools(FrameBuffer(), FakeVLM(), Timeline(), WatchRules(), EventBus())
        await Agent(llm, tools).run("有哪些提醒", lambda _: None)
        assert llm.tool_choices == ["required", "auto"]
        llm2 = FakeLLM(["好"])
        await Agent(llm2, tools, force_tool_first=False).run("x", lambda _: None)
        assert llm2.tool_choices == ["auto"]
    asyncio.run(run())


# ---------------------------------------------------------------- 长期记忆
def _drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _memory(tmp_path, llm=None, vlm=None, **over):
    cfg = load_config(None)["memory"]
    cfg.update({"dir": str(tmp_path / "mem"), "look_max_tokens": 128, **over})
    bus = EventBus()
    return LongTermMemory(cfg, bus, llm or FakeLLM([]), vlm or FakeVLM()), bus


def test_parse_time():
    now = _at(15, 30)
    assert parse_time("09:00", now) == _at(9, 0)
    assert parse_time("2026-11-13 09:05", now) == datetime(2026, 11, 13, 9, 5).timestamp()
    assert parse_time("11-12 18:00", now) == datetime(datetime.now().year, 11, 12, 18, 0).timestamp()
    assert parse_time("24:00", now) == _at(0, 0, 1)
    with pytest.raises(ValueError):
        parse_time("上午九点", now)
    assert window_start(_at(9, 7) + 30, 10) == _at(9, 0)


def test_recorder_persists_bus_events(tmp_path):
    async def run():
        mem, bus = _memory(tmp_path)
        bus.bind_loop(asyncio.get_running_loop())
        task = asyncio.create_task(mem.recorder.run())
        await asyncio.sleep(0)
        entry = TimelineEntry(ts=time.time(), summary="一人走进门", people_count=1, activities=["走路"])
        bus.publish(TOPIC_OBSERVATION, {"entry": entry, "jpeg": _jpeg()})
        bus.publish(TOPIC_ALERT, {"rule_id": "r1", "condition": "有人进门", "evidence": "门口有人", "ts": time.time()})
        bus.publish(TOPIC_UI, {"type": "user", "text": "有人进门就提醒我", "source": "voice"})
        bus.publish(TOPIC_UI, {"type": "assistant_delta", "text": "忽略"})
        await asyncio.sleep(0.3)
        task.cancel()
        events = mem.store.events_between(time.time() - 60, time.time() + 1)
        kinds = sorted(e.kind for e in events)
        assert kinds == sorted([KIND_OBSERVATION, KIND_ALERT, KIND_COMMAND])
        obs = next(e for e in events if e.kind == KIND_OBSERVATION)
        assert obs.people == 1 and obs.details["activities"] == ["走路"]
        saved = mem.keyframes.load(obs.frame)
        assert saved and saved[:2] == b"\xff\xd8"
        await mem.close()
    asyncio.run(run())


def _seed_morning(mem):
    """今天 08:50~09:20 每分钟一条画面记录，09:06 有人离开，并保存关键帧。"""
    for m in range(50, 81):
        ts = _at(8, 0) + m * 60
        summary = "一人离开工位走向门口" if m == 66 else ("两人在工位办公" if m < 66 else "一人在工位办公")
        frame = mem.keyframes.save(ts, _jpeg(100 + m))
        mem.store.add_event(MemoryEvent(ts=ts, kind=KIND_OBSERVATION, summary=summary,
                                        people=2 if m < 66 else 1, frame=frame))
    mem.store.add_event(MemoryEvent(ts=_at(9, 6) + 5, kind=KIND_ALERT, summary="提醒：有人离开座位"))


def test_search_memory_by_time(tmp_path):
    async def run():
        mem, _ = _memory(tmp_path, max_events=10)
        _seed_morning(mem)
        r = json.loads(await mem.tools.call("search_memory", {"start": "08:45", "end": "09:15"}))
        assert len(r["records"]) <= 10
        assert any("提醒：有人离开座位" in x for x in r["records"])       # 提醒不会被抽样掉
        assert "已均匀抽取" in r["note"]
        r = json.loads(await mem.tools.call("search_memory", {"start": "08:45", "end": "09:15", "keyword": "离开"}))
        assert [x for x in r["records"] if "离开工位" in x]
        r = json.loads(await mem.tools.call("search_memory", {"start": "03:00", "end": "04:00"}))
        assert r["records"] == [] and "现有记录范围" in r["note"]
        r = json.loads(await mem.tools.call("search_memory", {"start": "上午", "end": "09:00"}))
        assert "error" in r
        await mem.close()
    asyncio.run(run())


def test_look_at_time(tmp_path):
    async def run():
        vlm = FakeVLM(default="门口有一个穿蓝衣服的人。")
        mem, _ = _memory(tmp_path, vlm=vlm)
        _seed_morning(mem)
        r = json.loads(await mem.tools.call("look_at_time", {"time": "09:06", "question": "门口有谁"}))
        assert r["answer"] == "门口有一个穿蓝衣服的人。" and r["record"] == "一人离开工位走向门口"
        assert vlm.requests and vlm.requests[0][0] == 1
        r = json.loads(await mem.tools.call("look_at_time", {"time": "03:00", "question": "x"}))
        assert "没有保存画面" in r["error"]
        await mem.close()
    asyncio.run(run())


def test_summarizer(tmp_path):
    async def run():
        llm = FakeLLM(["09:00-09:10 两人办公，09:06 一人离开。"] * 10)
        mem, _ = _memory(tmp_path, llm=llm, summary_max_per_tick=10)
        _seed_morning(mem)
        now = _at(9, 25)
        pending = mem.summarizer.pending_windows(now)
        assert pending[:3] == [_at(9, 10), _at(9, 0), _at(8, 50)]
        for start in pending:
            await mem.summarizer.summarize(start)
        assert mem.summarizer.pending_windows(now) == []
        prompt = llm.calls[1][1]["content"]
        assert "09:06:00 一人离开工位走向门口" in prompt
        r = json.loads(await mem.tools.call("search_memory", {"start": "09:00", "end": "09:10"}))
        assert any("09:06 一人离开" in s for s in r["summaries"])
        await mem.close()
    asyncio.run(run())


def test_notes_and_retention(tmp_path):
    async def run():
        mem, _ = _memory(tmp_path, retention_days=30, keyframe_retention_days=7)
        r = json.loads(await mem.tools.call("remember_note", {"content": "3 号工位是张工的"}))
        assert r["note_id"] == "n1" and mem.tools.note_lines() == ["[n1] 3 号工位是张工的"]
        assert json.loads(await mem.tools.call("forget_note", {"note_id": "n1"}))["ok"]
        assert mem.tools.note_lines() == []
        now = time.time()
        old_frame = mem.keyframes.save(now - 10 * 86400, _jpeg())
        mem.store.add_event(MemoryEvent(ts=now - 10 * 86400, kind=KIND_OBSERVATION, summary="十天前", frame=old_frame))
        mem.store.add_event(MemoryEvent(ts=now - 40 * 86400, kind=KIND_OBSERVATION, summary="四十天前"))
        mem.recorder.cleanup(now)
        left = mem.store.events_between(0, now)
        assert [e.summary for e in left] == ["十天前"] and left[0].frame is None
        assert mem.keyframes.load(old_frame) is None
        await mem.close()
    asyncio.run(run())


def test_rules_survive_restart(tmp_path):
    async def run():
        mem, _ = _memory(tmp_path)
        rules = WatchRules()
        mem.attach_rules(rules)
        rules.add("画面中没有人", cooldown_s=120)
        rules.add("有人进门")
        rules.remove("r1")
        await mem.close()
        mem2, _ = _memory(tmp_path)
        rules2 = WatchRules()
        mem2.attach_rules(rules2)
        assert [(r.id, r.condition) for r in rules2.list()] == [("r2", "有人进门")]
        assert rules2.add("有人离开").id == "r3"                         # 编号接着往后排
        await mem2.close()
    asyncio.run(run())


def test_app_records_to_memory(tmp_path):
    async def run():
        from vva.app import App
        cfg = load_config(None)
        cfg["video"].update({"type": "custom", "custom": {
            "class": os.path.join(ROOT, "examples", "custom_video_source.py") + ":TestPatternSource",
            "params": {"fps": 20}}})
        cfg["observer"]["interval"] = 0.3
        cfg["memory"]["dir"] = str(tmp_path / "memory")
        obs_json = json.dumps({"summary": "一块表盘", "people_count": 0, "objects": ["表盘"],
                               "activities": [], "anomalies": [], "triggered_rules": []}, ensure_ascii=False)
        llm = FakeLLM([[("search_memory", {"start": "00:00", "end": "23:59"})],
                       "画面中有一块表盘，一直没有人。后面这句话很长很长很长很长很长很长很长很长很长很长很长。"])
        app = App(cfg, llm=llm, vlm=FakeVLM(answers={"监控画面": obs_json}), enable_audio=False)
        app.cfg["agent"]["max_answer_chars"] = 12
        ui = app.bus.subscribe(TOPIC_UI)
        await app.start()
        await asyncio.sleep(0.8)
        await app.handle_user_text("今天发生了什么")
        await app._turn
        await asyncio.sleep(0.1)
        kinds = {e.kind for e in app.memory.store.events_between(0, time.time() + 1)}
        assert {KIND_OBSERVATION, KIND_COMMAND} <= kinds
        tool_result = json.loads(next(m for m in app.agent.history if m["role"] == "tool")["content"])
        assert any("一块表盘" in x for x in tool_result["records"])
        said = "".join(e["text"] for e in _drain(ui) if e["type"] == "assistant_delta")
        assert said == "画面中有一块表盘，一直没有人。"                     # 超长部分被截掉
        assert "search_memory" in {t["function"]["name"] for t in app.tools.schemas()}
        await app.stop()
    asyncio.run(run())


# ---------------------------------------------------------------- 看图问答格式
def test_look_answer_format_and_scene_hint():
    from vva.perception.look import parse_look_answer
    r = parse_look_answer("依据：表盘指针在 0.3 和 0.4 之间偏左\n结论：读数约 0.32 毫米\n把握：中")
    assert r == {"evidence": "表盘指针在 0.3 和 0.4 之间偏左", "answer": "读数约 0.32 毫米", "confidence": "中"}
    assert parse_look_answer("<think>x</think>画面里有两个人。") == {"answer": "画面里有两个人。"}
    assert parse_look_answer("**结论**：两人\n把握：低（太远）")["confidence"] == "低"

    async def run():
        from vva.agent.tools import VisionTools
        from vva.core.types import Frame
        from vva.perception.timeline import Timeline
        from vva.video.frame_buffer import FrameBuffer
        buf = FrameBuffer()
        buf.add(Frame(image=np.full((360, 640, 3), 90, np.uint8), ts=time.time()))
        vlm = FakeVLM(default="依据：工位上坐着两人\n结论：画面中有 2 人\n把握：高")
        tools = VisionTools(buf, vlm, Timeline(), WatchRules(), EventBus(), scene_hint="办公室，左侧是 3 号工位")
        r = json.loads(await tools.call("look_now", {"question": "几个人"}))
        assert r["answer"] == "画面中有 2 人" and r["confidence"] == "高"
        assert "场景说明：办公室，左侧是 3 号工位" in vlm.requests[0][1] and "问题：几个人" in vlm.requests[0][1]
    asyncio.run(run())


def test_look_now_zooms_by_target_not_by_model_coordinates():
    async def run():
        from vva.agent.tools import VisionTools
        from vva.core.types import Frame
        from vva.perception.timeline import Timeline
        from vva.video.frame_buffer import FrameBuffer
        buf = FrameBuffer()
        buf.add(Frame(image=np.full((720, 1280, 3), 90, np.uint8), ts=time.time()))
        vlm = FakeVLM(answers={"保温杯": '{"found": true, "objects": [{"label": "保温杯", "box_2d": [100, 500, 300, 900]}]}',
                               "表盘": '{"found": false, "objects": []}'},
                      default="依据：x\n结论：粉色保温杯，约 20 厘米高\n把握：中")
        bus = EventBus()
        ui = bus.subscribe(TOPIC_UI)
        tools = VisionTools(buf, vlm, Timeline(), WatchRules(), bus, crop_min_side=0)
        r = json.loads(await tools.call("look_now", {"question": "杯子多大", "target": "保温杯"}))
        assert r["zoomed"] == "已定位并放大：保温杯" and r["answer"].startswith("粉色保温杯")
        assert [e["boxes"][0]["box"] for e in _drain(ui) if e["type"] == "bbox"] == [[0.1, 0.5, 0.3, 0.9]]
        r = json.loads(await tools.call("look_now", {"question": "读数", "target": "表盘"}))
        assert "没有定位到“表盘”，已改看整幅画面" == r["zoomed"]
        # 模型自己编的坐标会被忽略，不再裁剪
        n = len(vlm.requests)
        r = json.loads(await tools.call("look_now", {"question": "杯子", "region": [0.45, 0.35, 0.65, 0.55]}))
        assert "zoomed" not in r and len(vlm.requests) == n + 1
        assert "region" not in json.dumps(tools.schemas(), ensure_ascii=False)
    asyncio.run(run())
