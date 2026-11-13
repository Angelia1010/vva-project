"""语音会话：麦克风 → VAD 断句 → ASR → 交给上层；上层文字 → TTS → 扬声器；支持打断和唤醒词。

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

log = logging.getLogger(__name__)


class VoiceSession:
    def __init__(self, audio_in: AudioInput, audio_out: AudioOutput, asr: Any, tts: Any,
                 vad: VADSegmenter, bus: EventBus,
                 on_utterance: Callable[[str], Awaitable[None]],
                 on_barge_in: Callable[[], Awaitable[None]] | None = None,
                 barge_in: bool = True, mute_mic_while_speaking: bool = True, mute_tail_ms: int = 400,
                 wake_words: list[str] | None = None, wake_timeout: float = 30.0, **_: Any) -> None:
        self.audio_in, self.audio_out, self.asr, self.tts, self.vad, self.bus = audio_in, audio_out, asr, tts, vad, bus
        self.on_utterance = on_utterance
        self.on_barge_in = on_barge_in
        self.barge_in = barge_in
        self.mute_while_speaking = mute_mic_while_speaking
        self.mute_tail = mute_tail_ms / 1000.0
        self.wake_words = [w for w in (wake_words or []) if w]
        self.wake_timeout = wake_timeout
        self._awake_until = 0.0
        self._mic_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        self._speak_q: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        self._gen = 0
        self._synth_busy = False
        self._last_speaking = 0.0
        self._tasks: list[asyncio.Task] = []

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
        log.info("语音会话已启动（打断=%s，播放时静音麦克风=%s，唤醒词=%s）",
                 self.barge_in, self.mute_while_speaking, self.wake_words or "无")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self.audio_in.stop()
        self.audio_out.stop()

    def _enqueue_mic(self, pcm: bytes) -> None:
        try:
            self._mic_q.put_nowait(pcm)
        except asyncio.QueueFull:
            pass

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
            return True
        return time.time() - self._last_speaking < self.mute_tail

    async def _listen_loop(self) -> None:
        while True:
            pcm = await self._mic_q.get()
            if self._mic_muted():
                if self.vad.in_speech:
                    self.vad.reset()
                continue
            for ev, data in self.vad.feed(pcm):
                if ev == "start":
                    self.bus.publish(TOPIC_UI, {"type": "vad", "state": "start"})
                    if self.barge_in and self.speaking:
                        log.info("检测到用户说话，打断播放")
                        await self.interrupt()
                        if self.on_barge_in:
                            await self.on_barge_in()
                elif ev == "cancel":
                    self.bus.publish(TOPIC_UI, {"type": "vad", "state": "end"})
                elif ev == "end" and data:
                    self.bus.publish(TOPIC_UI, {"type": "vad", "state": "end"})
                    asyncio.create_task(self._handle_utterance(data))

    def _check_wake(self, text: str) -> str | None:
        """返回去掉唤醒词后的文本；未唤醒时返回 None。"""
        if not self.wake_words:
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
        if not text or not any(ch.isalnum() for ch in text):
            return
        content = self._check_wake(text)
        if content is None:
            self.bus.publish(TOPIC_UI, {"type": "asr", "text": text, "ignored": True})
            return
        if not content:  # 只说了唤醒词
            self.say("我在。")
            return
        await self.on_utterance(content)
