"""推送式音频输入/输出：给外部音频引擎使用。

- PushAudioInput：外部调用 push_pcm(bytes) 把麦克风数据送进来；
- CallbackAudioOutput：要播放的 PCM 通过回调交给外部（WebSocket、SIP、对讲 SDK 等）。

配置 audio.input.type=push / audio.output.type=push 后，网页服务会开放
WebSocket 接口 /api/ingest/audio，协议见 README。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from vva.audio.base import AudioInput, AudioOutput


class PushAudioInput(AudioInput):
    def __init__(self, sample_rate: int = 16000, **kw: Any) -> None:
        super().__init__(sample_rate=sample_rate)
        self._running = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def push_pcm(self, pcm: bytes) -> None:
        if self._running and pcm:
            if len(pcm) % 2:
                pcm = pcm[:-1]
            self._emit(pcm)


class CallbackAudioOutput(AudioOutput):
    def __init__(self, sample_rate: int = 24000, **kw: Any) -> None:
        super().__init__(sample_rate=sample_rate)
        self._sinks: list[Callable[[bytes], None]] = []
        self._clear_listeners: list[Callable[[], None]] = []
        self._play_until = 0.0

    def add_sink(self, fn: Callable[[bytes], None]) -> None:
        self._sinks.append(fn)

    def remove_sink(self, fn: Callable[[bytes], None]) -> None:
        if fn in self._sinks:
            self._sinks.remove(fn)

    def add_clear_listener(self, fn: Callable[[], None]) -> None:
        self._clear_listeners.append(fn)

    def remove_clear_listener(self, fn: Callable[[], None]) -> None:
        if fn in self._clear_listeners:
            self._clear_listeners.remove(fn)

    def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        # 估算外部播放完成的时间，用于判断“系统是否正在说话”
        now = time.time()
        self._play_until = max(now, self._play_until) + len(pcm) / 2 / self.sample_rate
        for fn in list(self._sinks):
            fn(pcm)

    def clear(self) -> None:
        self._play_until = 0.0
        for fn in list(self._clear_listeners):
            fn()

    def is_playing(self) -> bool:
        return time.time() < self._play_until
