"""电脑本地麦克风和扬声器（基于 sounddevice），用于本地测试。"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from vva.audio.base import AudioInput, AudioOutput

log = logging.getLogger(__name__)


def _sd():
    try:
        import sounddevice as sd
        return sd
    except Exception as e:  # pragma: no cover
        raise RuntimeError("本地音频需要 sounddevice：pip install sounddevice"
                           "（Linux 还需 apt install libportaudio2）") from e


def list_devices() -> str:
    return str(_sd().query_devices())


class LocalMicrophone(AudioInput):
    def __init__(self, sample_rate: int = 16000, device: Any = None, block_ms: int = 30, **kw: Any) -> None:
        super().__init__(sample_rate=sample_rate)
        self.device = device
        self.block = int(self.sample_rate * block_ms / 1000)
        self._stream = None

    def _callback(self, indata, frames, time_info, status) -> None:  # 在 PortAudio 线程中执行
        if status:
            log.debug("麦克风状态: %s", status)
        self._emit(bytes(indata))

    def start(self) -> None:
        sd = _sd()
        self._stream = sd.RawInputStream(samplerate=self.sample_rate, blocksize=self.block,
                                         channels=1, dtype="int16", device=self.device,
                                         callback=self._callback)
        self._stream.start()
        log.info("本地麦克风已开启: device=%s, %d Hz", self.device, self.sample_rate)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class LocalSpeaker(AudioOutput):
    """独立线程播放；clear() 通过“代号”机制在 20ms 内停止当前播放。"""

    def __init__(self, sample_rate: int = 24000, device: Any = None, **kw: Any) -> None:
        super().__init__(sample_rate=sample_rate)
        self.device = device
        self._q: queue.Queue[tuple[int, bytes]] = queue.Queue()
        self._gen = 0
        self._playing = False
        self._running = False
        self._thread: threading.Thread | None = None
        self._chunk = int(self.sample_rate * 0.02) * 2  # 20ms 的字节数

    def start(self) -> None:
        if self._running:
            return
        _sd()  # 依赖缺失时在主线程直接报错，便于上层降级为纯文字模式
        self._running = True
        self._thread = threading.Thread(target=self._run, name="speaker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self.clear()
        if self._thread:
            self._thread.join(timeout=2)

    def write(self, pcm: bytes) -> None:
        if pcm:
            self._q.put((self._gen, pcm))

    def clear(self) -> None:
        self._gen += 1
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def is_playing(self) -> bool:
        return self._playing or not self._q.empty()

    def _run(self) -> None:
        sd = _sd()
        with sd.RawOutputStream(samplerate=self.sample_rate, channels=1, dtype="int16",
                                device=self.device) as stream:
            log.info("本地扬声器已开启: device=%s, %d Hz", self.device, self.sample_rate)
            while self._running:
                try:
                    gen, pcm = self._q.get(timeout=0.1)
                except queue.Empty:
                    self._playing = False
                    continue
                if gen != self._gen:
                    continue
                self._playing = True
                for i in range(0, len(pcm), self._chunk):
                    if gen != self._gen or not self._running:
                        break
                    stream.write(pcm[i:i + self._chunk])
                if self._q.empty():
                    self._playing = False
