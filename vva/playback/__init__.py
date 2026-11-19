"""回放：按“摄像头 + 时间”从录像里取片段、取某一秒的画面。只读录像，不管录像怎么录。

  pb = Playback(store, root)
  path = pb.clip("cam1", start, end)      # 网页能直接播放的 MP4（H.264 直接拼接，其他编码转成 H.264）
  jpeg = pb.frame("cam1", t)              # 那一秒的画面

H.265 等编码在网页上兼容性差，所以只要不是 H.264 就转码：优先用显卡（h264_nvenc），不可用时用 CPU（libx264）。
生成的片段放在 <录像目录>/_cache/ 下，1 小时后清理。
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

MAX_CLIP_S = 600.0


class NoRecording(Exception):
    """这段时间没有录像。"""


class Playback:
    def __init__(self, store: Any, root: str, encoder: str = "auto") -> None:
        self.store, self.root = store, root
        self.cache_dir = os.path.join(root, "_cache")
        self._encoder = None if encoder == "auto" else encoder
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(2)          # 同时转码最多 2 个，避免占满 CPU / 显卡编码器

    # ---------------------------------------------------------------- 编码器
    def encoder(self) -> str:
        """可用的 H.264 编码器：先试显卡，失败用 CPU。只检测一次。"""
        with self._lock:
            if self._encoder:
                return self._encoder
            exe = shutil.which("ffmpeg")
            enc = "libx264"
            if exe:
                try:
                    r = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                        "color=c=black:s=256x144:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
                                       capture_output=True, timeout=20)
                    if r.returncode == 0:
                        enc = "h264_nvenc"
                except (subprocess.TimeoutExpired, OSError):
                    pass
            self._encoder = enc
            log.info("录像回放转码使用 %s", enc)
            return enc

    def _video_args(self) -> list[str]:
        if self.encoder() == "h264_nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "28", "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p"]

    # ---------------------------------------------------------------- 片段
    def _segments(self, camera: str, start: float, end: float) -> list[dict[str, Any]]:
        segs = [s for s in self.store.segments(camera, start, end) if os.path.isfile(s["path"])]
        if not segs:
            raise NoRecording(f"{camera} 在这段时间没有录像")
        return segs

    def clip(self, camera: str, start: float, end: float, out: str | None = None, transcode: bool | None = None) -> str:
        """[start, end] 的片段，返回文件路径。transcode=None 时按编码自动决定。"""
        end = min(end, start + MAX_CLIP_S)
        if end <= start:
            raise ValueError("结束时间要晚于开始时间")
        segs = self._segments(camera, start, end)
        need = transcode if transcode is not None else any(s["codec"] != "h264" for s in segs)
        key = hashlib.sha1(f"{camera}|{start:.1f}|{end:.1f}|{need}|{len(segs)}|{segs[-1]['end']:.1f}".encode()).hexdigest()[:16]
        out = out or os.path.join(self.cache_dir, f"{camera}-{key}.mp4")
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            os.utime(out)
            return out
        os.makedirs(os.path.dirname(out), exist_ok=True)
        first = segs[0]["start"]
        offset = max(0.0, start - first)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            for s in segs:
                f.write("file '" + os.path.abspath(s["path"]).replace("'", r"'\''") + "'\n")
            listfile = f.name
        tmp = out + ".part.mp4"

        def command() -> list[str]:
            # -ss 放在输入前：直接拼接时从最近的关键帧开始（可能早零点几秒），转码时精确到帧
            cmd = [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", f"{offset:.2f}", "-f", "concat", "-safe", "0", "-i", listfile, "-t", f"{end - start:.2f}",
                   "-map", "0:v:0", "-map", "0:a:0?", "-avoid_negative_ts", "make_zero"]
            cmd += (self._video_args() + ["-c:a", "aac", "-b:a", "48k"]) if need else ["-c", "copy"]
            return cmd + ["-movflags", "+faststart", tmp]

        try:
            with self._sem:
                r = subprocess.run(command(), capture_output=True, timeout=300)
            if r.returncode != 0 and need and self.encoder() == "h264_nvenc":     # 显卡编码出错时退回 CPU
                log.warning("显卡转码失败，改用 CPU：%s", r.stderr.decode(errors="replace")[-200:])
                self._encoder = "libx264"
                with self._sem:
                    r = subprocess.run(command(), capture_output=True, timeout=300)
            if r.returncode != 0 or not os.path.isfile(tmp):
                raise RuntimeError("生成片段失败：" + r.stderr.decode(errors="replace").strip()[-200:])
            os.replace(tmp, out)
            return out
        finally:
            for p in (listfile, tmp):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def frame(self, camera: str, t: float, max_side: int = 1280) -> bytes:
        """t 时刻的画面（JPEG）。"""
        segs = self._segments(camera, t - 0.01, t + 0.01)
        s = segs[0]
        offset = min(max(0.0, t - s["start"]), max(0.0, s["end"] - s["start"] - 0.05))
        cmd = [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-ss", f"{offset:.2f}", "-i", s["path"], "-frames:v", "1",
               "-vf", f"scale='min({max_side},iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "pipe:1"]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError("取画面失败：" + r.stderr.decode(errors="replace").strip()[-200:])
        return r.stdout

    def clean_cache(self, max_age_s: float = 3600) -> int:
        n = 0
        if not os.path.isdir(self.cache_dir):
            return 0
        now = time.time()
        for name in os.listdir(self.cache_dir):
            p = os.path.join(self.cache_dir, name)
            try:
                if now - os.path.getmtime(p) > max_age_s:
                    os.remove(p)
                    n += 1
            except OSError:
                pass
        return n


__all__ = ["MAX_CLIP_S", "NoRecording", "Playback"]
