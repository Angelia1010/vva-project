"""应用编排：按配置组装各模块，并负责一轮对话的调度（打断、流式输出、主动提醒）。

各模块之间只通过接口和事件总线连接：
  视频源 ──帧──▶ 帧缓存 ◀── 工具 / 后台观察 ──▶ 多模态大模型
  语音会话 ──文字──▶ App.handle_user_text ──▶ 智能体 ──文字──▶ 语音会话 / 网页
  后台观察 ──疑似命中──▶ 职责引擎 ──复核──▶ 告警（data/ops.db）──▶ 通知路由 ──事件──▶ 钉钉 / 飞书 / 值守台 / 现场播报
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
from vva.perception.timeline import Timeline
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

        # 岗位 + 职责（data/ops.db）
        from vva.core.sqlite import SqliteDB
        from vva.duties import DutyManager
        from vva.station import StationStore
        self.ops_db = SqliteDB(cfg["duties"]["db"])
        self.station = StationStore(self.ops_db, cfg.get("station") or {})
        self.timeline = Timeline(cfg["observer"]["timeline_minutes"])
        self.duties = DutyManager(cfg["duties"], self.ops_db, self.bus, self.buffer, self.vlm, self.llm,
                                  report=self.report, scene=self.scene_text, station_name=lambda: self.station.station.name,
                                  observe_interval=cfg["observer"].get("rule_check_interval", 8.0))
        from vva.actions import ActionService
        self.actions = ActionService(self.ops_db, self.bus, shots_dir=cfg.get("actions", {}).get("dir", "data/actions"),
                                     speak=self._say_on_site, notify=self.send_notice, snapshot=self.snapshot,
                                     station_name=lambda: self.station.station.name, problem=self._action_problem)
        self.rules = self.duties.book           # 职责清单兼容旧的监控规则接口（后台观察、技能使用）
        self.rules.paused = self.station.station.paused
        vision_tools = VisionTools(self.buffer, self.vlm, self.timeline, self.rules, self.bus, **cfg["agent"])

        # 技能（可关闭）
        self.skills = None
        if cfg["skills"].get("enabled", True):
            from vva.skills import SkillManager
            self.skills = SkillManager(cfg["skills"], cfg["video"].get("source_id", "cam0"))
            self.skills.load_plugins(self.buffer, self.vlm, self.bus)

        # 长期记忆（可关闭）
        self.memory = None
        if cfg["memory"].get("enabled", True):
            from vva.memory import LongTermMemory
            mem_cfg = {"look_max_tokens": cfg["agent"]["look_max_tokens"],
                       "scene_hint": cfg["agent"].get("scene_hint", ""), **cfg["memory"]}
            self.memory = LongTermMemory(mem_cfg, self.bus, self.llm, self.vlm)
            if not len(self.rules):             # 旧版的监控规则存在长期记忆里：首次启动时转成职责
                n = self.rules.load(self.memory.store.load_rules())
                if n:
                    log.info("已把 %d 条旧监控规则转成职责", n)
        # 领域知识库子智能体（可关闭）
        self.knowledge = None
        if cfg["knowledge"].get("enabled", True):
            from vva.knowledge import KnowledgeBase
            embedder = clients.EmbeddingModel(m["embedding"]) if m["embedding"].get("model") else None
            self.knowledge = KnowledgeBase(cfg["knowledge"], self.llm, embedder, self.bus)

        skill_tools = None
        if self.skills:
            from vva.skills import SkillTools
            skill_tools = SkillTools(self.skills, self.buffer, self.vlm, self.bus, **cfg["agent"])
            self.skills.apply_rules(self.rules)
            self.skills.on_change(lambda: self.skills.apply_rules(self.rules))   # 网页上新增的规则模板立即生效
            self.duties.verifier.references = self.skills.references_for_text
        self.tools = ToolSet(vision_tools, self.duties.tools, self.actions.tools, self.memory.tools if self.memory else None, skill_tools,
                             self.knowledge.tools if self.knowledge else None)
        self.agent = Agent(self.llm, self.tools, **cfg["agent"],
                           notes_provider=self.memory.tools.note_lines if self.memory else None)
        self.agent.extra_prompt = self._extra_prompt
        if self.skills:
            self.agent.tool_visible = self.skills.tool_visible
        self.conversations = ConversationStore(cfg["agent"]["max_history_turns"])
        obs_cfg = dict(cfg["observer"])
        if not obs_cfg.get("scene_hint"):
            obs_cfg["scene_hint"] = cfg["agent"].get("scene_hint", "")
        obs_cfg["hint_provider"] = self._observer_hint
        if self.skills:
            obs_cfg["references"] = self.skills.references_for_text
        obs_cfg["on_hit"] = self.duties.engine.on_hit
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
            if self.skills:     # 技能里的热词、目标名作为语音识别热词，修改后自动更新
                base_prompt = m["asr"].get("prompt") or ""

                def _update_hotwords() -> None:
                    words = self.skills.asr_prompt()
                    m["asr"]["prompt"] = "，".join(x for x in (base_prompt, words) if x)
                _update_hotwords()
                self.skills.on_change(_update_hotwords)
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
        from vva.channels import ChannelManager
        self.channels = ChannelManager(cfg.get("channels") or {}, self, cfg.get("channels_settings_dir") or "data/channels")
        self.duties.target_options = self.notify_targets
        self.duties.target_problem = self.target_problem
        self.rules.on_change(lambda _: self._announce_duties())

    DUTY_NOTE = "【职责清单已更新】"

    def _announce_duties(self) -> None:
        """职责增删改后，告诉每个会话最新的清单，避免模型沿用对话里旧的数量。"""
        text = f"现在{self.duties.summary()}。"
        for conv in self.conversations.all():
            conv.replace_note(self.DUTY_NOTE, text)

    # ---------------------------------------------------------------- 岗位、场景、通知对象
    def scene_text(self) -> str:
        st = self.station.station
        return "；".join(x for x in (st.area, st.scene, self.cfg["agent"].get("scene_hint", "")) if x)

    def _observer_hint(self) -> str:
        parts = [self.station.station.prompt_block()]
        if self.skills:
            parts.append(self.skills.observer_hint())
        return "\n".join(p for p in parts if p)

    def _extra_prompt(self) -> str:
        st = self.station.station
        block = f"\n## 岗位\n{st.prompt_block()}\n" + (f"岗位负责人：{st.owner}\n" if st.owner else "")
        return block + self.duties.prompt_block() + self.actions.prompt_block() + (self.skills.prompt_block() if self.skills else "")

    def notify_targets(self) -> list[dict[str, str]]:
        """可选的通知对象：值守台、现场播报、各入口的默认告警群和最近发过消息的群。"""
        opts = [{"id": "web", "label": "值守台"}, {"id": "voice", "label": "现场播报"}]
        labels = {"dingtalk": "钉钉", "feishu": "飞书"}
        for name in self.channels.names():
            cfg = self.channels.config(name)
            if not cfg.get("enabled"):
                continue
            label = labels.get(name, name)
            if cfg.get("alert_chats") and not any(o["id"] == "default" for o in opts):
                opts.append({"id": "default", "label": "默认告警群"})
            ch = self.channels.get(name)
            recent = {r["chat_id"]: r for r in (ch.status().get("recent", []) if ch and hasattr(ch, "status") else [])}
            chats = list(dict.fromkeys([*(cfg.get("alert_chats") or []), *(cfg.get("report_chats") or []),
                                        *(c for c, r in recent.items() if r.get("chat_type") == "group")]))
            used = [t.split(":", 1)[1] for d in self.rules.list() for t in d.targets if t.startswith(name + ":")]
            for chat in dict.fromkeys([*chats, *used]):
                title = (recent.get(chat) or {}).get("title") or ""
                opts.append({"id": f"{name}:{chat}", "label": f"{label}·{title or chat[:12]}"})
        return opts

    # ---------------------------------------------------------------- 执行动作用到的现场能力
    def _say_on_site(self, text: str) -> None:
        if self.voice is None:
            raise RuntimeError("现场播报未开启")
        self.voice.say(text)

    def _action_problem(self, kind: str) -> str:
        if kind == "speaker":
            return "" if self.voice is not None else "现场播报未开启"
        if kind == "notify":
            enabled = [n for n in self.channels.names() if self.channels.config(n).get("enabled")]
            if not enabled:
                return "钉钉/飞书未开启"
            if not any(self.channels.config(n).get("alert_chats") for n in enabled):
                return "没有设置默认告警群"
        return ""

    async def send_notice(self, title: str, text: str) -> list[str]:
        """给各入口的默认告警群发一条通知，返回发到了哪里；一个都没发出去则抛异常。"""
        labels, sent, errors = {"dingtalk": "钉钉", "feishu": "飞书"}, [], []
        for name in self.channels.names():
            cfg, ch = self.channels.config(name), self.channels.get(name)
            if not cfg.get("enabled") or ch is None:
                continue
            for chat in cfg.get("alert_chats") or []:
                try:
                    await ch.send_report_to(chat, title, text)
                    sent.append(f"{labels.get(name, name)}告警群")
                except Exception as e:
                    errors.append(f"{labels.get(name, name)}：{e}")
        if not sent:
            raise RuntimeError("；".join(errors) or "没有设置默认告警群")
        return list(dict.fromkeys(sent))

    def target_problem(self, target: str) -> str:
        """通知对象发不出去的原因；没问题返回空串。"""
        if target == "voice" and self.voice is None and self.audio_out is None:
            return "现场播报未开启"
        if target == "default":
            enabled = [n for n in self.channels.names() if self.channels.config(n).get("enabled")]
            if enabled and not any(self.channels.config(n).get("alert_chats") for n in enabled):
                return "钉钉、飞书都没有设置默认告警群"
            return ""
        ch, _, _ = target.partition(":")
        if ch in ("dingtalk", "feishu"):
            label = {"dingtalk": "钉钉", "feishu": "飞书"}[ch]
            if not self.channels.config(ch).get("enabled"):
                return f"{label}未开启"
            c = self.channels.get(ch)
            if c is not None and getattr(c, "started", True) is False:
                return f"{label}未连接"
        return ""

    def alert_image(self, rel: str) -> bytes | None:
        path = self.duties.alerts.store.image_path(rel)
        if not path:
            return None
        with open(path, "rb") as f:
            return f.read()

    def notify_result(self, alert_id: int | None, target: str, ok: bool, error: str = "") -> None:
        """接入入口发送告警后回报结果，记进告警的处理经过。"""
        if alert_id:
            text = f"已送达 {target}" if ok else f"发送到 {target} 失败：{error}"
            self.duties.alerts.add_event(int(alert_id), text)

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        self.bus.bind_loop(asyncio.get_running_loop())
        self.video.start()
        log.info("视频源：%s", self.video.describe())
        if self.observer:
            self._tasks.append(asyncio.create_task(self.observer.run(), name="observer"))
        self._tasks.append(asyncio.create_task(self._alert_loop(), name="alerts"))
        self._tasks += [asyncio.create_task(c) for c in self.duties.background_tasks()]
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
        await self.channels.start()

    async def stop(self) -> None:
        await self.cancel_turn()
        await self.channels.stop()
        for t in self._tasks:
            t.cancel()
        if self.voice:
            await self.voice.stop()
        self.video.stop()
        if self.memory:
            await self.memory.close()
        if self.knowledge:
            await self.knowledge.close()
        await self.duties.close()
        self.ops_db.close()

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
        if self.observer:                   # 问答优先：后台观察让出一个周期的模型
            self.observer.hold(self.observer.interval * 2)
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
        """值守台和现场播报：告警的 targets 里有 web / voice 时处理（旧版没有 targets 的提醒两者都做）。"""
        q = self.bus.subscribe(TOPIC_ALERT)
        while True:
            alert = await q.get()
            if alert.get("kind", "alert") != "alert":
                continue
            targets = alert.get("targets")
            text = f"提醒：{alert['condition']}。{alert.get('evidence') or ''}".strip()
            log.info(text)
            if targets is None or "web" in targets:
                self.bus.publish(TOPIC_UI, {"type": "alert", "text": text, **alert})
                self.add_note(WEB_SESSION, text)
            if self.voice and (targets is None or "voice" in targets):
                self.voice.say(alert.get("voice_text") or text)

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
            "station": self.station.to_dict(),
            "stats": self.duties.alerts.stats(),
        }
