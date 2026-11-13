"""语音会话：麦克风 → 断句 → ASR → 过滤 → 交给上层；上层文字 → TTS → 扬声器；支持打断。

三种收音模式（voice.mode）：
  always        一直在听，VAD 自动断句
  wake_word     一直在听，但句子里要带唤醒词才响应；唤醒后 wake_timeout 秒内的后续指令不必再说
  push_to_talk  按住说话：只有按下期间的音频会被识别（由网页的“按住说话”按钮或空格键控制）

本模块只依赖 AudioInput / AudioOutput / asr.transcribe / tts.stream 这几个接口，
不关心音频从哪来、到哪去，也不关心智能体如何回答。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from vva.audio.base import AudioInput, AudioOutput
from vva.audio.vad import VADSegmenter
from vva.core.bus import TOPIC_UI, EventBus
from vva.voice.filters import UtteranceFilter

log = logging.getLogger(__name__)

MODES = ("always", "wake_word", "push_to_talk")
_PTT_DOWN, _PTT_UP = "ptt_down", "ptt_up"


class VoiceSession:
    def __init__(self, audio_in: AudioInput, audio_out: AudioOutput, asr: Any, tts: Any,
                 vad: VADSegmenter, bus: EventBus,
                 on_utterance: Callable[[str], Awaitable[None]],
                 on_barge_in: Callable[[], Awaitable[None]] | None = None,
                 mode: str = "always", barge_in: bool = True, mute_mic_while_speaking: bool = True,
                 mute_tail_ms: int = 400, wake_words: list[str] | None = None, wake_timeout: float = 30.0,
                 min_chars: int = 2, ignore_phrases: list[str] | None = None,
                 ptt_min_ms: int = 300, ptt_tail_ms: int = 200, **_: Any) -> None:
        self.audio_in, self.audio_out, self.asr, self.tts, self.vad, self.bus = audio_in, audio_out, asr, tts, vad, bus
        self.on_utterance = on_utterance
        self.on_barge_in = on_barge_in
        self.barge_in = barge_in
        self.mute_while_speaking = mute_mic_while_speaking
        self.mute_tail = mute_tail_ms / 1000.0
        self.wake_words = [w for w in (wake_words or []) if w]
        self.wake_timeout = wake_timeout
        self.mode = self._resolve_mode(mode)
        self.filter = UtteranceFilter(min_chars, ignore_phrases)
        self.ptt_min_bytes = int(audio_in.sample_rate * ptt_min_ms / 1000) * 2
        self.ptt_tail = ptt_tail_ms / 1000.0
        self._ptt_pressed = False
        self._ptt_buf: list[bytes] = []
        self._awake_until = 0.0
        self._mic_q: asyncio.Queue[bytes | str] = asyncio.Queue(maxsize=500)
        self._speak_q: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        self._gen = 0
        self._synth_busy = False
        self._last_speaking = 0.0
        self._muted_logged = False
        self._tasks: list[asyncio.Task] = []

    def _resolve_mode(self, mode: str) -> str:
        if mode not in MODES:
            log.warning("未知的 voice.mode=%s，改用 always", mode)
            return "always"
        if mode == "wake_word" and not self.wake_words:
            log.warning("voice.mode=wake_word 但没有配置 voice.wake_words，改用 always")
            return "always"
        return mode

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        def _on_audio(pcm: bytes) -> None:  # 可能在音频线程里调用
            loop.call_soon_threadsafe(self._enqueue_mic, pcm)

        self.audio_in.on_audio(_on_audio)
        self.audio_out.start()
        self.audio_in.start()
        self._tasks = [asyncio.create_task(self._listen_loop(), name="voice-listen"),
                       asyncio.create_task(self._speak_loop(), name="voice-speak")]
        log.info("语音会话已启动（模式=%s，打断=%s，播放时静音麦克风=%s，唤醒词=%s）", self.mode,
                 self.barge_in, self.mute_while_speaking, self.wake_words if self.mode == "wake_word" else "不使用")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self.audio_in.stop()
        self.audio_out.stop()

    def _enqueue_mic(self, item: bytes | str) -> None:
        try:
            self._mic_q.put_nowait(item)
        except asyncio.QueueFull:
            pass

    # ---------------------------------------------------------------- 按住说话
    def ptt_press(self) -> None:
        """按下“按住说话”。只在 push_to_talk 模式下生效。"""
        if self.mode == "push_to_talk":
            self._enqueue_mic(_PTT_DOWN)

    def ptt_release(self) -> None:
        """松开。稍等片刻再结束，让还在路上的音频包先到。"""
        if self.mode == "push_to_talk":
            asyncio.get_running_loop().call_later(self.ptt_tail, self._enqueue_mic, _PTT_UP)

    # ---------------------------------------------------------------- 播放
    @property
    def speaking(self) -> bool:
        return self._synth_busy or not self._speak_q.empty() or self.audio_out.is_playing()

    def say(self, sentence: str) -> None:
        """把一句话加入播放队列（会被 interrupt() 取消）。"""
        if sentence.strip():
            self._speak_q.put_nowait((self._gen, sentence))

    async def interrupt(self) -> None:
        """立即停止说话：作废队列中的句子、停止正在合成和播放的音频。"""
        self._gen += 1
        while not self._speak_q.empty():
            self._speak_q.get_nowait()
        self.audio_out.clear()
        self.bus.publish(TOPIC_UI, {"type": "tts", "state": "stopped"})

    async def _speak_loop(self) -> None:
        while True:
            gen, sentence = await self._speak_q.get()
            if gen != self._gen:
                continue
            self._synth_busy = True
            self.bus.publish(TOPIC_UI, {"type": "tts", "state": "speaking", "text": sentence})
            try:
                async for pcm in self.tts.stream(sentence):
                    if gen != self._gen:
                        break
                    self.audio_out.write(pcm)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("语音合成失败：%s", e)
                self.bus.publish(TOPIC_UI, {"type": "error", "message": f"语音合成失败：{e}"})
            finally:
                self._synth_busy = False
                self._last_speaking = time.time()

    # ---------------------------------------------------------------- 收听
    def _mic_muted(self) -> bool:
        if not self.mute_while_speaking:
            return False
        if self.speaking:
            self._last_speaking = time.time()
            muted = True
        else:
            muted = time.time() - self._last_speaking < self.mute_tail
        if muted != self._muted_logged:
            self._muted_logged = muted
            log.info("播报中，暂停收音" if muted else "播报结束，恢复收音")
        return muted

    async def _listen_loop(self) -> None:
        while True:
            item = await self._mic_q.get()
            if self.mode == "push_to_talk":
                await self._handle_ptt(item)
            elif isinstance(item, bytes):
                await self._handle_vad(item)

    async def _barge_in(self) -> None:
        if self.barge_in and self.speaking:
            log.info("检测到用户说话，打断播放")
            await self.interrupt()
            if self.on_barge_in:
                await self.on_barge_in()

    async def _handle_vad(self, pcm: bytes) -> None:
        if self._mic_muted():
            if self.vad.in_speech:
                self.vad.reset()
            return
        for ev, data in self.vad.feed(pcm):
            if ev == "start":
                self.bus.publish(TOPIC_UI, {"type": "vad", "state": "start"})
                await self._barge_in()
            elif ev == "cancel":
                self.bus.publish(TOPIC_UI, {"type": "vad", "state": "end"})
            elif ev == "end" and data:
                self.bus.publish(TOPIC_UI, {"type": "vad", "state": "end"})
                asyncio.create_task(self._handle_utterance(data))

    async def _handle_ptt(self, item: bytes | str) -> None:
        if item == _PTT_DOWN:
            self._ptt_pressed, self._ptt_buf = True, []
            self.bus.publish(TOPIC_UI, {"type": "vad", "state": "start"})
            # 按下即表示要下新指令：停止播报，并取消还没回答完的上一条
            if self.speaking:
                await self.interrupt()
            if self.on_barge_in:
                await self.on_barge_in()
        elif item == _PTT_UP:
            if not self._ptt_pressed:
                return
            self._ptt_pressed = False
            pcm, self._ptt_buf = b"".join(self._ptt_buf), []
            self.bus.publish(TOPIC_UI, {"type": "vad", "state": "end"})
            if len(pcm) >= self.ptt_min_bytes:
                asyncio.create_task(self._handle_utterance(pcm))
        elif self._ptt_pressed and isinstance(item, bytes):
            self._ptt_buf.append(item)

    def _check_wake(self, text: str) -> str | None:
        """返回去掉唤醒词后的文本；未唤醒时返回 None。只在 wake_word 模式下检查。"""
        if self.mode != "wake_word":
            return text
        now = time.time()
        for w in self.wake_words:
            if w in text:
                self._awake_until = now + self.wake_timeout
                rest = text.split(w, 1)[1].lstrip("，,。.！!？? ")
                return rest or ""
        if now < self._awake_until:
            self._awake_until = now + self.wake_timeout
            return text
        return None

    async def _handle_utterance(self, pcm: bytes) -> None:
        t0 = time.time()
        try:
            text = await self.asr.transcribe(pcm, self.audio_in.sample_rate)
        except Exception as e:
            log.warning("语音识别失败：%s", e)
            self.bus.publish(TOPIC_UI, {"type": "error", "message": f"语音识别失败：{e}"})
            return
        log.info("识别结果（%.2fs）：%s", time.time() - t0, text)
        reason = self.filter.reason_to_drop(text)
        if reason:
            log.info("忽略识别结果（%s）：%s", reason, text)
            self.bus.publish(TOPIC_UI, {"type": "asr", "text": text, "ignored": True, "reason": reason})
            return
        content = self._check_wake(text)
        if content is None:
            self.bus.publish(TOPIC_UI, {"type": "asr", "text": text, "ignored": True, "reason": "未唤醒"})
            return
        if not content:  # 只说了唤醒词
            self.say("我在。")
            return
        await self.on_utterance(content)
