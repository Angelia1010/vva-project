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
from dataclasses import dataclass, field
from typing import Any

from vva.agent.agent import Agent
from vva.agent.conversation import ConversationStore
from vva.agent.toolkit import ToolSet
from vva.agent.tools import VisionTools
from vva.core import turn
from vva.core.bus import TOPIC_ALERT, TOPIC_UI, EventBus
from vva.perception.observer import Observer
from vva.perception.timeline import Timeline, WatchRules
from vva.video.factory import create_video_source
from vva.video.frame_buffer import FrameBuffer
from vva.voice.text_split import AnswerLimiter, SentenceSplitter, ThinkFilter

log = logging.getLogger(__name__)

WEB_SESSION = "web"     # 网页（含语音）共用的会话


@dataclass
class Reply:
    """一轮问答的结果，供飞书等入口使用。"""
    text: str
    attachments: list[turn.Attachment] = field(default_factory=list)
    interrupted: bool = False
    error: str | None = None


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

        # 场景包（可关闭）
        self.scene = None
        if cfg["scene"].get("enabled", True):
            from vva.scene import SceneManager
            self.scene = SceneManager(cfg["scene"])

        # 长期记忆（可关闭）
        self.memory = None
        if cfg["memory"].get("enabled", True):
            from vva.memory import LongTermMemory
            mem_cfg = {"look_max_tokens": cfg["agent"]["look_max_tokens"],
                       "scene_hint": cfg["agent"].get("scene_hint", ""), **cfg["memory"]}
            self.memory = LongTermMemory(mem_cfg, self.bus, self.llm, self.vlm)
            self.memory.attach_rules(self.rules)
        # 领域知识库子智能体（可关闭）
        self.knowledge = None
        if cfg["knowledge"].get("enabled", True):
            from vva.knowledge import KnowledgeBase
            embedder = clients.EmbeddingModel(m["embedding"]) if m["embedding"].get("model") else None
            self.knowledge = KnowledgeBase(cfg["knowledge"], self.llm, embedder, self.bus)

        if self.scene:
            self.scene.apply_rules(self.rules)
            self.scene.on_change(lambda: self.scene.apply_rules(self.rules))   # 网页上新增的规则模板立即生效
        self.tools = ToolSet(vision_tools, self.memory.tools if self.memory else None,
                             self.scene.tools(self.buffer, self.vlm, self.bus, **cfg["agent"]) if self.scene else None,
                             self.knowledge.tools if self.knowledge else None)
        self.agent = Agent(self.llm, self.tools, **cfg["agent"],
                           notes_provider=self.memory.tools.note_lines if self.memory else None)
        if self.scene:
            self.agent.extra_prompt = self.scene.ctx.prompt_block
        self.conversations = ConversationStore(cfg["agent"]["max_history_turns"])
        obs_cfg = dict(cfg["observer"])
        if not obs_cfg.get("scene_hint"):
            obs_cfg["scene_hint"] = cfg["agent"].get("scene_hint", "")
        if self.scene:
            obs_cfg["hint_provider"] = self.scene.ctx.observer_hint
            obs_cfg["references"] = self.scene.ctx.references_for_text
        self.observer = Observer(self.buffer, self.vlm, self.timeline, self.rules, self.bus, **obs_cfg) \
            if obs_cfg.get("enabled", True) else None

        # 语音
        audio_on = cfg["audio"]["enabled"] if enable_audio is None else enable_audio
        self.asr = asr
        self.voice = None
        self.audio_in = self.audio_out = None
        if audio_on:
            from vva.audio.factory import create_audio_input, create_audio_output
            from vva.audio.vad import VADSegmenter
            from vva.voice.session import VoiceSession
            self.asr = asr or self._make_asr()
            if self.scene:     # 场景包里的术语、目标名作为语音识别热词，场景包修改后自动更新
                base_prompt = m["asr"].get("prompt") or ""

                def _update_hotwords() -> None:
                    words = self.scene.ctx.asr_prompt()
                    m["asr"]["prompt"] = "，".join(x for x in (base_prompt, words) if x)
                _update_hotwords()
                self.scene.on_change(_update_hotwords)
            self.tts = tts or clients.TextToSpeech(m["tts"])
            self.audio_in = create_audio_input(cfg["audio"]["input"])
            self.audio_out = create_audio_output(cfg["audio"]["output"], self.tts.sample_rate)
            vad = VADSegmenter(sample_rate=self.audio_in.sample_rate, **cfg["vad"])
            self.voice = VoiceSession(self.audio_in, self.audio_out, self.asr, self.tts, vad, self.bus,
                                      on_utterance=lambda t: self.handle_user_text(t, "voice", WEB_SESSION),
                                      on_barge_in=self.cancel_turn, **cfg["voice"])

        self._turns: dict[str, asyncio.Task] = {}
        self._tasks: list[asyncio.Task] = []

        # 接入入口（飞书等），只通过 ChannelHost 接口与应用交互
        from vva.channels import build_channels
        self.channels = build_channels(cfg.get("channels") or {}, self)

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
        if self.knowledge:
            self._tasks += [asyncio.create_task(c) for c in self.knowledge.background_tasks()]
        if self.voice:
            try:
                await self.voice.start()
            except Exception as e:
                log.error("语音模块启动失败，将只支持文字交互：%s", e)
                self.voice = None
        for ch in self.channels:
            try:
                await ch.start()
            except Exception as e:
                log.error("接入入口 %s 启动失败：%s", ch.name, e)

    async def stop(self) -> None:
        await self.cancel_turn()
        for ch in self.channels:
            with contextlib.suppress(Exception):
                await ch.stop()
        for t in self._tasks:
            t.cancel()
        if self.voice:
            await self.voice.stop()
        self.video.stop()
        if self.memory:
            await self.memory.close()
        if self.knowledge:
            await self.knowledge.close()

    # ---------------------------------------------------------------- 对话
    @property
    def _turn(self) -> asyncio.Task | None:
        """网页会话当前的回合（兼容旧接口）。"""
        return self._turns.get(WEB_SESSION)

    @property
    def busy(self) -> bool:
        t = self._turns.get(WEB_SESSION)
        return t is not None and not t.done()

    async def cancel_turn(self, session: str = WEB_SESSION) -> None:
        t = self._turns.pop(session, None)
        if t is not None and not t.done():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    async def interrupt(self, session: str = WEB_SESSION) -> None:
        await self.cancel_turn(session)
        if session == WEB_SESSION and self.voice:
            await self.voice.interrupt()

    def reset_session(self, session: str = WEB_SESSION) -> None:
        conv = self.conversations.peek(session)
        if conv:
            conv.reset()

    def add_note(self, session: str, text: str) -> None:
        """把系统主动说过的话（如告警）记进某个会话，之后可以接着追问。"""
        self.conversations.get(session).add_note(text)

    async def _start(self, text: str, source: str, session: str, readonly: bool) -> asyncio.Task | None:
        text = text.strip()
        if not text:
            return None
        await self.interrupt(session)       # 同一会话里，新的输入会打断正在进行的回答
        self.bus.publish(TOPIC_UI, {"type": "user", "text": text, "source": source, "session": session})
        speak = session == WEB_SESSION and self.voice is not None
        task = asyncio.create_task(self._run_turn(text, session, speak, readonly))
        self._turns[session] = task
        return task

    async def handle_user_text(self, text: str, source: str = "text", session: str = WEB_SESSION) -> None:
        """网页文字、语音的入口：开始回答后立即返回，回答通过事件流式推送。"""
        await self._start(text, source, session, readonly=False)

    async def ask(self, text: str, session: str, source: str = "api", readonly: bool = False) -> Reply:
        """飞书等入口：等待回答完成，返回文字和本轮看过的画面。"""
        task = await self._start(text, source, session, readonly)
        if task is None:
            return Reply("")
        try:
            return await task
        except asyncio.CancelledError:
            return Reply("", interrupted=True)

    async def _run_turn(self, text: str, session: str, speak: bool, readonly: bool) -> Reply:
        attachments = turn.begin(session)
        conv = self.conversations.get(session, readonly)
        splitter, think = SentenceSplitter(), ThinkFilter()
        limiter = AnswerLimiter(self.cfg["agent"].get("max_answer_chars", 0))
        answer: list[str] = []
        self.bus.publish(TOPIC_UI, {"type": "assistant_start"})

        def on_text(delta: str) -> None:
            visible = limiter.feed(think.feed(delta))
            if not visible:
                return
            answer.append(visible)
            self.bus.publish(TOPIC_UI, {"type": "assistant_delta", "text": visible})
            if speak and self.voice:
                for s in splitter.feed(visible):
                    self.voice.say(s)

        log.info("问[%s]：%s", session, text)
        try:
            await self.agent.run(text, on_text, conversation=conv)
            on_text(think.flush())
            if speak and self.voice:
                for s in splitter.flush():
                    self.voice.say(s)
            self.bus.publish(TOPIC_UI, {"type": "assistant_end"})
            log.info("答[%s]：%s", session, "".join(answer))
            return Reply("".join(answer), attachments)
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
            return Reply(msg, attachments, error=str(e))

    # ---------------------------------------------------------------- 供接入入口使用（ChannelHost）
    def _make_asr(self) -> Any:
        from vva.models import clients
        return clients.SpeechToText(self.cfg["models"]["asr"])

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str | None:
        """语音转文字。没开网页语音时也可用（如飞书语音消息），只要配置了 models.asr。"""
        if self.asr is None:
            if not self.cfg["models"]["asr"].get("model"):
                return None
            self.asr = self._make_asr()
        return await self.asr.transcribe(pcm, sample_rate)

    async def report(self, start: float, end: float) -> str:
        if not self.memory:
            return "未开启长期记忆，无法生成报告。"
        from vva.memory.report import build_report
        return await build_report(self.memory.store, self.llm, start, end)

    def snapshot(self, max_side: int = 1280) -> bytes | None:
        """当前画面的 JPEG，供告警推送等使用。"""
        bf = self.buffer.latest_snapshot(max_side=max_side, quality=85)
        return bf.jpeg if bf else None

    # ---------------------------------------------------------------- 主动提醒
    async def _alert_loop(self) -> None:
        q = self.bus.subscribe(TOPIC_ALERT)
        while True:
            alert = await q.get()
            text = f"提醒：{alert['condition']}。{alert.get('evidence') or ''}".strip()
            log.info(text)
            self.bus.publish(TOPIC_UI, {"type": "alert", "text": text, **alert})
            self.add_note(WEB_SESSION, text)
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
            "knowledge": self.knowledge.stats() if self.knowledge else None,
            "voice_mode": self.voice.mode if self.voice else None,
            "rules": [r.to_dict() for r in self.rules.list()],
        }
