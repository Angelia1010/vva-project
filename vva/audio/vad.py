"""语音活动检测（VAD）+ 断句。

把连续的麦克风 PCM 切成一句一句，输出事件：
  ("start", None)   检测到开始说话（用于打断）
  ("end", pcm)      一句话结束，pcm 为这句话的完整音频（含少量前置音频）

默认用“能量 + 自适应噪声底”的方法，无需额外依赖；
若安装了 webrtcvad 且 engine=auto/webrtc，则自动使用 webrtcvad，抗噪更好。
"""
from __future__ import annotations

import logging
from collections import deque

from vva.audio.utils import rms_dbfs

log = logging.getLogger(__name__)


class VADSegmenter:
    def __init__(self, sample_rate: int = 16000, frame_ms: int = 30, threshold_db: float = -45.0,
                 margin_db: float = 12.0, start_frames: int = 3, end_silence_ms: int = 700,
                 pre_roll_ms: int = 300, min_utterance_ms: int = 300, max_utterance_s: float = 15.0,
                 engine: str = "auto", webrtc_mode: int = 2, **_: object) -> None:
        if frame_ms not in (10, 20, 30):
            frame_ms = 30
        self.sr = sample_rate
        self.frame_bytes = int(sample_rate * frame_ms / 1000) * 2
        self.frame_ms = frame_ms
        self.threshold_db = threshold_db
        self.margin_db = margin_db
        self.start_frames = start_frames
        self.end_frames = max(1, end_silence_ms // frame_ms)
        self.pre_frames = max(0, pre_roll_ms // frame_ms)
        self.min_frames = max(1, min_utterance_ms // frame_ms)
        self.max_frames = int(max_utterance_s * 1000 / frame_ms)
        self.noise_db = -60.0
        self._webrtc = None
        if engine in ("auto", "webrtc") and sample_rate in (8000, 16000, 32000, 48000):
            try:
                import webrtcvad
                self._webrtc = webrtcvad.Vad(int(webrtc_mode))
                log.info("VAD 使用 webrtcvad")
            except Exception:
                if engine == "webrtc":
                    log.warning("未安装 webrtcvad，改用能量 VAD")
        self.reset()

    def reset(self) -> None:
        self._pending = b""
        self._pre: deque[bytes] = deque(maxlen=self.pre_frames or 1)
        self._speech: list[bytes] = []
        self._in_speech = False
        self._voiced_run = 0
        self._silence_run = 0

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def _is_speech(self, frame: bytes) -> bool:
        db = rms_dbfs(frame)
        if self._webrtc is not None:
            try:
                return self._webrtc.is_speech(frame, self.sr) and db > self.threshold_db - 10
            except Exception:
                pass
        speech = db > max(self.threshold_db, self.noise_db + self.margin_db)
        if not speech and not self._in_speech:
            # 只在安静时更新噪声底，适应环境噪声
            self.noise_db = 0.95 * self.noise_db + 0.05 * db
        return speech

    def feed(self, pcm: bytes) -> list[tuple[str, bytes | None]]:
        events: list[tuple[str, bytes | None]] = []
        data = self._pending + pcm
        n = len(data) // self.frame_bytes
        self._pending = data[n * self.frame_bytes:]
        for i in range(n):
            frame = data[i * self.frame_bytes:(i + 1) * self.frame_bytes]
            speech = self._is_speech(frame)
            if not self._in_speech:
                self._pre.append(frame)
                self._voiced_run = self._voiced_run + 1 if speech else 0
                if self._voiced_run >= self.start_frames:
                    self._in_speech = True
                    self._speech = list(self._pre)
                    self._silence_run = 0
                    events.append(("start", None))
            else:
                self._speech.append(frame)
                self._silence_run = 0 if speech else self._silence_run + 1
                too_long = len(self._speech) >= self.max_frames
                if self._silence_run >= self.end_frames or too_long:
                    voiced = len(self._speech) - self._silence_run
                    audio = b"".join(self._speech)
                    self._in_speech = False
                    self._speech = []
                    self._voiced_run = 0
                    self._pre.clear()
                    if voiced >= self.min_frames:
                        events.append(("end", audio))
                    else:
                        events.append(("cancel", None))
        return events
