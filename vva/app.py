"""应用编排：按配置组装各模块，并负责一轮对话的调度（打断、流式输出、主动提醒）。

各模块之间只通过接口和事件总线连接：
  视频源 ──帧──▶ 帧缓存 ◀── 工具 / 后台观察 ──▶ 多模态大模型
  语音会话 ──文字──▶ App.handle_user_text ──▶ 智能体 ──文字──▶ 语音会话 / 网页
  后台观察 / 提醒 / 指令 ──事件──▶ 长期记忆（SQLite + 关键帧）◀── 记忆工具
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from vva.agent.agent import Agent
from vva.agent.toolkit import ToolSet
from vva.agent.tools import VisionTools
from vva.core.bus import TOPIC_ALERT, TOPIC_UI, EventBus
from vva.perception.observer import Observer
from vva.perception.timeline import Timeline, WatchRules
from vva.video.factory import create_video_source
from vva.video.frame_buffer import FrameBuffer
from vva.voice.text_split import AnswerLimiter, SentenceSplitter, ThinkFilter

log = logging.getLogger(__name__)


class App:
    def __init__(self, cfg: dict[str, Any], llm: Any = None, vlm: Any = None,
                 asr: Any = None, tts: Any = None, enable_audio: bool | None = None) -> None:
        """llm/vlm/asr/tts 可从外部注入（测试或自定义实现），否则按配置创建 OpenAI 格式客户端。"""
        self.cfg = cfg
        self.bus = EventBus()

        # 视频
        self.video = create_video_source(cfg["video"])
        self.buffer = FrameBuffer(**cfg["video"]["buffer"])
        self.video.on_frame(self.buffer.add)

        # 模型
        from vva.models import clients
        m = cfg["models"]
        self.llm = llm or clients.ChatModel(m["agent_llm"])
        self.vlm = vlm or clients.VisionModel(m["vlm"])

        # 感知 + 智能体
        self.timeline = Timeline(cfg["observer"]["timeline_minutes"])
        self.rules = WatchRules()
        vision_tools = VisionTools(self.buffer, self.vlm, self.timeline, self.rules, self.bus, **cfg["agent"])

        # 长期记忆（可关闭）
        self.memory = None
        if cfg["memory"].get("enabled", True):
            from vva.memory import LongTermMemory
            mem_cfg = {"look_max_tokens": cfg["agent"]["look_max_tokens"], **cfg["memory"]}
            self.memory = LongTermMemory(mem_cfg, self.bus, self.llm, self.vlm)
            self.memory.attach_rules(self.rules)
        self.tools = ToolSet(vision_tools, self.memory.tools if self.memory else None)
        self.agent = Agent(self.llm, self.tools, **cfg["agent"],
                           notes_provider=self.memory.tools.note_lines if self.memory else None)
        obs_cfg = dict(cfg["observer"])
        if not obs_cfg.get("scene_hint"):
            obs_cfg["scene_hint"] = cfg["agent"].get("scene_hint", "")
        self.observer = Observer(self.buffer, self.vlm, self.timeline, self.rules, self.bus, **obs_cfg) \
            if obs_cfg.get("enabled", True) else None

        # 语音
        audio_on = cfg["audio"]["enabled"] if enable_audio is None else enable_audio
        self.voice = None
        self.audio_in = self.audio_out = None
        if audio_on:
            from vva.audio.factory import create_audio_input, create_audio_output
            from vva.audio.vad import VADSegmenter
            from vva.voice.session import VoiceSession
            self.asr = asr or clients.SpeechToText(m["asr"])
            self.tts = tts or clients.TextToSpeech(m["tts"])
            self.audio_in = create_audio_input(cfg["audio"]["input"])
            self.audio_out = create_audio_output(cfg["audio"]["output"], self.tts.sample_rate)
            vad = VADSegmenter(sample_rate=self.audio_in.sample_rate, **cfg["vad"])
            self.voice = VoiceSession(self.audio_in, self.audio_out, self.asr, self.tts, vad, self.bus,
                                      on_utterance=lambda t: self.handle_user_text(t, "voice"),
                                      on_barge_in=self.cancel_turn, **cfg["voice"])

        self._turn: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        self.bus.bind_loop(asyncio.get_running_loop())
        self.video.start()
        log.info("视频源：%s", self.video.describe())
        if self.observer:
            self._tasks.append(asyncio.create_task(self.observer.run(), name="observer"))
        self._tasks.append(asyncio.create_task(self._alert_loop(), name="alerts"))
        if self.memory:
            self._tasks += [asyncio.create_task(c) for c in self.memory.background_tasks()]
        if self.voice:
            try:
                await self.voice.start()
            except Exception as e:
                log.error("语音模块启动失败，将只支持文字交互：%s", e)
                self.voice = None

    async def stop(self) -> None:
        await self.cancel_turn()
        for t in self._tasks:
            t.cancel()
        if self.voice:
            await self.voice.stop()
        self.video.stop()
        if self.memory:
            await self.memory.close()

    # ---------------------------------------------------------------- 对话
    @property
    def busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    async def cancel_turn(self) -> None:
        if self.busy:
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn
        self._turn = None

    async def interrupt(self) -> None:
        await self.cancel_turn()
        if self.voice:
            await self.voice.interrupt()

    async def handle_user_text(self, text: str, source: str = "text") -> None:
        """文字和语音的统一入口。新的输入会打断正在进行的回答。"""
        text = text.strip()
        if not text:
            return
        await self.interrupt()
        self.bus.publish(TOPIC_UI, {"type": "user", "text": text, "source": source})
        self._turn = asyncio.create_task(self._run_turn(text, speak=self.voice is not None))

    async def _run_turn(self, text: str, speak: bool) -> None:
        splitter, think = SentenceSplitter(), ThinkFilter()
        limiter = AnswerLimiter(self.cfg["agent"].get("max_answer_chars", 0))
        self.bus.publish(TOPIC_UI, {"type": "assistant_start"})

        def on_text(delta: str) -> None:
            visible = limiter.feed(think.feed(delta))
            if not visible:
                return
            self.bus.publish(TOPIC_UI, {"type": "assistant_delta", "text": visible})
            if speak and self.voice:
                for s in splitter.feed(visible):
                    self.voice.say(s)

        try:
            await self.agent.run(text, on_text)
            on_text(think.flush())
            if speak and self.voice:
                for s in splitter.flush():
                    self.voice.say(s)
            self.bus.publish(TOPIC_UI, {"type": "assistant_end"})
        except asyncio.CancelledError:
            self.bus.publish(TOPIC_UI, {"type": "assistant_end", "interrupted": True})
            raise
        except Exception as e:
            log.exception("对话处理失败")
            msg = "抱歉，处理时出错了，请稍后再试。"
            self.bus.publish(TOPIC_UI, {"type": "assistant_delta", "text": msg})
            self.bus.publish(TOPIC_UI, {"type": "assistant_end", "error": str(e)})
            if speak and self.voice:
                self.voice.say(msg)

    # ---------------------------------------------------------------- 主动提醒
    async def _alert_loop(self) -> None:
        q = self.bus.subscribe(TOPIC_ALERT)
        while True:
            alert = await q.get()
            text = f"提醒：{alert['condition']}。{alert.get('evidence') or ''}".strip()
            log.info(text)
            self.bus.publish(TOPIC_UI, {"type": "alert", "text": text, **alert})
            self.agent.add_note(text)
            if self.voice:
                self.voice.say(text)

    # ---------------------------------------------------------------- 状态
    def status(self) -> dict[str, Any]:
        return {
            "video": {"source": self.video.describe(), "running": self.video.running, **self.buffer.stats()},
            "voice": self.voice is not None,
            "speaking": bool(self.voice and self.voice.speaking),
            "busy": self.busy,
            "observer": self.observer is not None,
            "timeline_entries": len(self.timeline),
            "memory": self.memory.stats() if self.memory else None,
            "voice_mode": self.voice.mode if self.voice else None,
            "rules": [r.to_dict() for r in self.rules.list()],
        }
