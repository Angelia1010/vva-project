"""每路摄像头的录制。

  StreamRecorder  网络摄像头：一个 ffmpeg 进程直接转存视频流（不重新编码），按分钟切片
  FrameRecorder   浏览器推送、本机 USB、自定义接入：没有独立的视频流，按帧从帧缓存写成 MP4（低帧率）

两者写完一片就回调 on_segment(路径, 开始, 结束, 大小, 编码, 低帧率)，由录像服务登记。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

SegmentCallback = Callable[[str, float, float, int, str, bool], None]


def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


def probe_codec(url: str, timeout: float = 15.0) -> dict[str, Any]:
    """{"video": "hevc", "audio": "aac", "width": 1920, "height": 1080}；失败返回空字典。"""
    exe = shutil.which("ffprobe")
    if not exe:
        return {}
    args = [exe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json"]
    if url.startswith("rtsp"):
        args += ["-rtsp_transport", "tcp"]
    try:
        out = subprocess.run(args + [url], capture_output=True, timeout=timeout, text=True).stdout
        info: dict[str, Any] = {}
        for s in json.loads(out or "{}").get("streams", []):
            if s.get("codec_type") == "video" and "video" not in info:
                info.update(video=s.get("codec_name", ""), width=s.get("width"), height=s.get("height"))
            elif s.get("codec_type") == "audio" and "audio" not in info:
                info["audio"] = s.get("codec_name", "")
        return info
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        log.warning("探测视频编码失败：%s", e)
        return {}


def _stamp(t: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(t))


def parse_stamp(name: str) -> float | None:
    try:
        return time.mktime(time.strptime(os.path.basename(name)[:15], "%Y%m%d-%H%M%S"))
    except ValueError:
        return None


class BaseRecorder:
    def __init__(self, camera: str, out_dir: str, on_segment: SegmentCallback, segment_s: float = 60.0) -> None:
        self.camera, self.out_dir, self.on_segment = camera, out_dir, on_segment
        self.segment_s = float(segment_s)
        self.error = ""
        self.last_segment = 0.0
        self.started = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        os.makedirs(self.out_dir, exist_ok=True)
        self._stop.clear()
        self.started = time.time()
        self._thread = threading.Thread(target=self._run, name=f"rec-{self.camera}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._thread = None

    def _run(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def _register(self, path: str, start: float, end: float, codec: str, low_fps: bool) -> None:
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size <= 0:
            return
        self.last_segment = end
        try:
            self.on_segment(path, start, end, size, codec, low_fps)
        except Exception:
            log.exception("登记录像切片失败")


class StreamRecorder(BaseRecorder):
    """ffmpeg -c copy 直存。断开后 5 秒重连。"""

    def __init__(self, camera: str, url: str, out_dir: str, on_segment: SegmentCallback, segment_s: float = 60.0,
                 retry_s: float = 5.0) -> None:
        super().__init__(camera, out_dir, on_segment, segment_s)
        self.url = url
        self.retry_s = float(retry_s)
        self.codec = ""
        self._proc: subprocess.Popen | None = None

    def command(self, codec: str = "") -> list[str]:
        exe = ffmpeg_bin() or "ffmpeg"
        args = [exe, "-nostdin", "-hide_banner", "-loglevel", "error"]
        is_file = os.path.isfile(self.url)
        if self.url.startswith("rtsp"):
            args += ["-rtsp_transport", "tcp"]
        if is_file:                      # 测试、演示用：视频文件循环播放，按实时速度读
            args += ["-re", "-stream_loop", "-1"]
        else:                            # 摄像头的时间戳经常不完整：用服务器时间
            args += ["-use_wallclock_as_timestamps", "1"]
        args += ["-fflags", "+genpts", "-i", self.url, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy"]
        if codec == "hevc":
            args += ["-tag:v", "hvc1"]   # 不加的话部分浏览器认不出 H.265 的 MP4
        args += ["-c:a", "aac", "-b:a", "32k",          # 摄像头的音频常是 G.711，MP4 放不下，转成 AAC（很小）
                 "-f", "segment", "-segment_time", str(int(self.segment_s)), "-segment_atclocktime", "1",
                 "-reset_timestamps", "1", "-strftime", "1", "-segment_format", "mp4",
                 "-segment_format_options", "movflags=+faststart",
                 "-segment_list", "pipe:1", "-segment_list_type", "csv",
                 os.path.join(self.out_dir, "%Y%m%d-%H%M%S.mp4")]
        return args

    def stop(self) -> None:
        self._stop.set()
        p = self._proc
        if p and p.poll() is None:
            try:
                p.stdin and p.stdin.close()
            except OSError:
                pass
            p.terminate()                # ffmpeg 收到 SIGTERM 会写完当前切片再退出
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
        super().stop()

    def _run(self) -> None:
        if not ffmpeg_bin():
            self.error = "服务器没有安装 ffmpeg"
            log.error("摄像头 %s 无法录像：%s", self.camera, self.error)
            return
        while not self._stop.is_set():
            if not self.codec:
                self.codec = probe_codec(self.url).get("video", "")
            cmd = self.command(self.codec)
            log.info("开始录像 %s：%s", self.camera, " ".join(a if "@" not in a else "<地址>" for a in cmd[:6]) + " …")
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            except OSError as e:
                self.error = f"启动 ffmpeg 失败：{e}"
                self._stop.wait(self.retry_s)
                continue
            errs: list[str] = []
            t = threading.Thread(target=self._drain, args=(self._proc.stderr, errs), daemon=True)
            t.start()
            self.error = ""
            for line in self._proc.stdout:      # 每写完一片输出一行：文件名,开始,结束（相对时间）
                self._on_line(line.strip())
            self._proc.wait()
            t.join(timeout=2)
            if self._stop.is_set():
                break
            self.error = (errs[-1] if errs else f"ffmpeg 退出（{self._proc.returncode}）")[:200]
            log.warning("摄像头 %s 录像中断：%s，%.0f 秒后重试", self.camera, self.error, self.retry_s)
            self._stop.wait(self.retry_s)

    @staticmethod
    def _drain(stream: Any, out: list[str]) -> None:
        for line in stream:
            line = line.strip()
            if line:
                out.append(line)
                del out[:-20]

    def _on_line(self, line: str) -> None:
        if not line:
            return
        parts = line.rsplit(",", 2)
        name = parts[0]
        path = name if os.path.isabs(name) else os.path.join(self.out_dir, name)
        start = parse_stamp(name)
        try:
            dur = float(parts[2]) - float(parts[1]) if len(parts) == 3 else self.segment_s
        except ValueError:
            dur = self.segment_s
        if start is None:
            start = time.time() - dur
        self._register(path, start, start + max(0.1, dur), self.codec, False)


class FrameRecorder(BaseRecorder):
    """从帧缓存取画面写成 MP4（mpeg4 编码，网页播放时会转码）。"""

    def __init__(self, camera: str, buffer: Any, out_dir: str, on_segment: SegmentCallback, segment_s: float = 60.0,
                 fps: float = 5.0) -> None:
        super().__init__(camera, out_dir, on_segment, segment_s)
        self.buffer = buffer
        self.fps = max(0.5, float(fps))

    def _run(self) -> None:
        """按固定帧率取最新一帧写入（画面没更新就重复上一帧），保证录像的时间和真实时间一致。"""
        import cv2
        writer = None
        path, seg_start, last_t, size, frames = "", 0.0, 0.0, None, 0

        def close() -> None:
            nonlocal writer, frames
            if writer is not None:
                writer.release()
                if frames:
                    self._register(path, seg_start, seg_start + frames / self.fps, "mpeg4", True)
                else:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            writer, frames = None, 0

        while not self._stop.is_set():
            t0 = time.time()
            f = self.buffer.latest_frame() if self.buffer else None
            if f is not None and t0 - f.ts < 5:
                h, w = f.image.shape[:2]
                boundary = writer is not None and int(t0 // self.segment_s) != int(seg_start // self.segment_s)
                if writer is not None and (boundary or (w, h) != size):
                    close()
                if writer is None:
                    seg_start, size = t0, (w, h)
                    path = os.path.join(self.out_dir, _stamp(t0) + ".mp4")
                    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
                    if not writer.isOpened():
                        self.error = "无法写入录像文件"
                        writer = None
                        self._stop.wait(5)
                        continue
                    self.error = ""
                writer.write(f.image)
                frames += 1
                last_t = t0
            elif writer is not None and t0 - last_t > 5:         # 超过 5 秒没有新画面：先收尾这一片
                close()
            self._stop.wait(max(0.01, 1.0 / self.fps - (time.time() - t0)))
        close()
