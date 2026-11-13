"""音频输入/输出抽象接口。

统一约定：PCM 16-bit 小端、单声道。
- 输入采样率由 audio.input.sample_rate 决定（默认 16000，语音识别常用）；
- 输出采样率与 TTS 的 sample_rate 一致（OpenAI 格式 pcm 默认 24000）。

接入其他音频引擎（对讲 SDK、SIP、会议系统……）只需继承下面两个类。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Callable

log = logging.getLogger(__name__)

AudioCallback = Callable[[bytes], None]


class AudioInput(ABC):
    def __init__(self, sample_rate: int = 16000, **_: object) -> None:
        self.sample_rate = int(sample_rate)
        self._callbacks: list[AudioCallback] = []

    def on_audio(self, cb: AudioCallback) -> None:
        """注册回调，参数为一段 PCM16 单声道数据。回调可能在任意线程执行。"""
        self._callbacks.append(cb)

    def _emit(self, pcm: bytes) -> None:
        for cb in self._callbacks:
            try:
                cb(pcm)
            except Exception:
                log.exception("音频回调异常")

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


class AudioOutput(ABC):
    def __init__(self, sample_rate: int = 24000, **_: object) -> None:
        self.sample_rate = int(sample_rate)

    @abstractmethod
    def write(self, pcm: bytes) -> None:
        """写入一段待播放的 PCM（非阻塞，进入播放队列）。"""

    @abstractmethod
    def clear(self) -> None:
        """立即停止播放并清空队列（用于打断）。"""

    @abstractmethod
    def is_playing(self) -> bool: ...

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class NullAudioOutput(AudioOutput):
    """不播放声音（只用文字交互时）。"""

    def write(self, pcm: bytes) -> None:
        pass

    def clear(self) -> None:
        pass

    def is_playing(self) -> bool:
        return False
