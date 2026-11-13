"""语音活动检测（VAD）+ 断句。

把连续的麦克风 PCM 切成一句一句，输出事件：
  ("start", None)   检测到开始说话（用于打断）
  ("end", pcm)      一句话结束，pcm 为这句话的完整音频（含少量前置音频）
  ("cancel", None)  开始后发现不是有效语音（太短或人声太少），丢弃

判断一帧是不是人声，依次做三项检查（任一不满足即判为非人声）：
  1. 音量：高于 threshold_db，且高于自适应噪声底 margin_db
  2. 频谱：人声能量集中在 80~4000Hz，键盘敲击、碰撞等脉冲声的能量分布更宽；
     该频带能量占比低于 min_band_ratio 的帧不算人声
  3. 若安装了 webrtcvad（engine=auto/webrtc），再由它做一次人声判断，抗噪更好

断句时再做两项过滤，挡住零星的敲击声：
  - 最近 start_window_ms 内至少有 start_frames 帧是人声才算“开始说话”
    （按比例而不是要求每帧都连续，正常说话字与字之间的弱音不会打断判断）
  - 一段音频里人声帧总时长不足 min_utterance_ms，或占比低于 min_voiced_ratio，就丢弃
"""
from __future__ import annotations

import logging
from collections import deque

import numpy as np

from vva.audio.utils import rms_dbfs

log = logging.getLogger(__name__)

SPEECH_BAND_HZ = (80.0, 4000.0)


def speech_band_ratio(frame: bytes, sample_rate: int) -> float:
    """一帧 PCM16 中 80~4000Hz 频带能量占总能量的比例（0~1）。"""
    x = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    if x.size == 0:
        return 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size))) ** 2
    total = float(spec[1:].sum())  # 去掉直流分量
    if total <= 0:
        return 0.0
    freqs = np.fft.rfftfreq(x.size, 1.0 / sample_rate)
    band = (freqs >= SPEECH_BAND_HZ[0]) & (freqs <= SPEECH_BAND_HZ[1])
    return float(spec[band].sum()) / total


class _Stats:
    def __init__(self) -> None:
        self.frames = self.loud = self.band = self.voiced = 0
        self.max_db = -120.0


class VADSegmenter:
    def __init__(self, sample_rate: int = 16000, frame_ms: int = 30, threshold_db: float = -40.0,
                 margin_db: float = 12.0, start_frames: int = 6, start_window_ms: int = 300,
                 end_silence_ms: int = 700,
                 pre_roll_ms: int = 300, min_utterance_ms: int = 500, min_voiced_ratio: float = 0.5,
                 min_band_ratio: float = 0.6, max_utterance_s: float = 15.0,
                 engine: str = "auto", webrtc_mode: int = 2, debug: bool = False, **_: object) -> None:
        if frame_ms not in (10, 20, 30):
            frame_ms = 30
        self.sr = sample_rate
        self.frame_bytes = int(sample_rate * frame_ms / 1000) * 2
        self.frame_ms = frame_ms
        self.threshold_db = threshold_db
        self.margin_db = margin_db
        self.start_window = max(1, start_window_ms // frame_ms)
        self.start_frames = min(max(1, int(start_frames)), self.start_window)
        self.end_frames = max(1, end_silence_ms // frame_ms)
        self.pre_frames = max(self.start_frames, pre_roll_ms // frame_ms)
        self.min_voiced_frames = max(1, min_utterance_ms // frame_ms)
        self.min_voiced_ratio = float(min_voiced_ratio)
        self.min_band_ratio = float(min_band_ratio)
        self.max_frames = int(max_utterance_s * 1000 / frame_ms)
        self.debug = bool(debug)
        self._stats = _Stats()
        self.noise_db = -60.0
        self._webrtc = None
        if engine in ("auto", "webrtc") and sample_rate in (8000, 16000, 32000, 48000):
            try:
                import webrtcvad
                self._webrtc = webrtcvad.Vad(int(webrtc_mode))
                log.info("VAD 使用 webrtcvad（模式 %s）", webrtc_mode)
            except Exception:
                level = logging.WARNING if engine == "webrtc" else logging.INFO
                log.log(level, "未安装 webrtcvad，使用能量+频谱 VAD（pip install webrtcvad-wheels 可提高抗噪）")
        self.reset()

    def reset(self) -> None:
        self._pending = b""
        self._pre: deque[bytes] = deque(maxlen=self.pre_frames)
        self._recent: deque[bool] = deque(maxlen=self.start_window)
        self._speech: list[bytes] = []
        self._in_speech = False
        self._voiced_total = 0
        self._silence_run = 0

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def _is_speech(self, frame: bytes) -> bool:
        db = rms_dbfs(frame)
        st = self._stats
        st.frames += 1
        st.max_db = max(st.max_db, db)
        loud = db > max(self.threshold_db, self.noise_db + self.margin_db)
        if not loud and not self._in_speech:
            # 只在安静时更新噪声底，适应环境噪声
            self.noise_db = 0.95 * self.noise_db + 0.05 * db
        if not loud:
            return False
        st.loud += 1
        if self.min_band_ratio > 0 and speech_band_ratio(frame, self.sr) < self.min_band_ratio:
            return False
        st.band += 1
        if self._webrtc is not None:
            try:
                if not self._webrtc.is_speech(frame, self.sr):
                    return False
            except Exception:
                pass
        st.voiced += 1
        return True

    def _log_stats(self) -> None:
        """debug=true 时每 2 秒打印一次检测统计，用于现场调参。"""
        st = self._stats
        if not self.debug or st.frames * self.frame_ms < 2000:
            return
        if st.loud:
            log.info("VAD 统计：最大音量 %.0fdB，噪声底 %.0fdB，门限 %.0fdB；%d 帧中 够响 %d / 人声频带 %d / 判为人声 %d",
                     st.max_db, self.noise_db, max(self.threshold_db, self.noise_db + self.margin_db),
                     st.frames, st.loud, st.band, st.voiced)
        self._stats = _Stats()

    def _finish(self, tail_silence: int) -> tuple[str, bytes | None]:
        # 人声占比按“开始说话到最后一次人声”统计，不算句尾的静音；送去识别的音频保留 300ms 句尾
        total = len(self._speech) - tail_silence
        keep = len(self._speech) - tail_silence + min(tail_silence, max(1, 300 // self.frame_ms))
        audio = b"".join(self._speech[:keep])
        voiced = self._voiced_total
        self._in_speech = False
        self._speech = []
        self._voiced_total = 0
        self._pre.clear()
        self._recent.clear()
        if voiced >= self.min_voiced_frames and voiced / max(1, total) >= self.min_voiced_ratio:
            return "end", audio
        log.info("丢弃一段声音：人声 %dms，占比 %.0f%%（要求至少 %dms、%.0f%%）", voiced * self.frame_ms,
                 100 * voiced / max(1, total), self.min_voiced_frames * self.frame_ms, 100 * self.min_voiced_ratio)
        return "cancel", None

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
                self._recent.append(speech)
                if speech and sum(self._recent) >= self.start_frames:
                    self._in_speech = True
                    self._speech = list(self._pre)
                    self._voiced_total = sum(self._recent)
                    self._silence_run = 0
                    events.append(("start", None))
            else:
                self._speech.append(frame)
                if speech:
                    self._voiced_total += 1
                    self._silence_run = 0
                else:
                    self._silence_run += 1
                if self._silence_run >= self.end_frames or len(self._speech) >= self.max_frames:
                    events.append(self._finish(self._silence_run))
        self._log_stats()
        return events
