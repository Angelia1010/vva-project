"""录像与回放的测试：切片登记、按天数和磁盘清理、告警片段、按帧录制、回放取片段和画面。

需要 ffmpeg 的测试在没有安装 ffmpeg 时跳过。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from collections import namedtuple

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vva.cameras import CameraHub, CameraStore  # noqa: E402
from vva.core.bus import TOPIC_ALERT, TOPIC_UI, EventBus  # noqa: E402
from vva.core.sqlite import SqliteDB  # noqa: E402
from vva.core.types import Frame  # noqa: E402
from vva.recording import DAY, RecordingService  # noqa: E402
from vva.recording.recorder import FrameRecorder, StreamRecorder, parse_stamp  # noqa: E402
from vva.station import StationStore  # noqa: E402

FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="没有安装 ffmpeg")


def _service(tmp_path, cams=("push",), **cfg):
    db = SqliteDB(":memory:")
    hub = CameraHub(CameraStore(db), StationStore(db, {"name": "1 号仓库"}), {"seconds": 10, "fps": 5})
    names = ["大门", "库房", "产线"]
    hub.seed([{"name": names[i], "source": ({"type": "push"} if t == "push" else {"type": "stream", "url": t})}
              for i, t in enumerate(cams)])
    conf = {"dir": str(tmp_path / "rec"), "segment_s": 60, "retention_days": 10,
            "alert_clip": {"before_s": 2, "after_s": 2, "retention_days": 180}, **cfg}
    return RecordingService(conf, db, hub, EventBus()), hub


def _seg(svc, cam, start, dur=60, size=1000, codec="h264"):
    d = os.path.join(svc.root, cam)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, time.strftime("%Y%m%d-%H%M%S", time.localtime(start)) + ".mp4")
    with open(p, "wb") as f:
        f.write(b"x" * size)
    svc.store.add_segment(cam, start, start + dur, p, size, codec)
    return p


def _h264_segments(svc, cam, start, n=3, dur=2):
    """用 ffmpeg 生成 n 段真正的 H.264 切片。"""
    d = os.path.join(svc.root, cam)
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        t = start + i * dur
        p = os.path.join(d, time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + f"{i}.mp4")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc=size=320x180:rate=10:duration={dur}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-g", "10", p], check=True)
        svc.store.add_segment(cam, t, t + dur, p, os.path.getsize(p), "h264")


# ---------------------------------------------------------------- 登记、时间轴
def test_store_segments_spans_and_usage(tmp_path):
    svc, _ = _service(tmp_path)
    t0 = time.time() - 3600
    for i in range(3):
        _seg(svc, "cam0", t0 + i * 60)
    _seg(svc, "cam0", t0 + 600)
    assert [round(b - a) for a, b in svc.store.spans("cam0", t0 - 10, t0 + 3600)] == [180, 60]
    assert len(svc.store.segments("cam0", t0 + 70, t0 + 80)) == 1
    assert svc.store.usage()["cam0"]["n"] == 4
    assert parse_stamp("20261119-105819.mp4") == time.mktime((2026, 11, 19, 10, 58, 19, 0, 0, -1))


def test_settings_defaults_and_overrides(tmp_path):
    svc, _ = _service(tmp_path)
    s = svc.settings()
    assert s["retention_days"] == 10 and s["alert_retention_days"] == 180 and s["clean_at"] == 85
    svc.update_settings({"retention_days": 3, "cameras": {"cam0": {"enabled": False, "retention_days": 1}}, "bogus": 1})
    assert svc.settings()["retention_days"] == 3 and "bogus" not in svc.settings()
    assert svc.camera_settings("cam0") == {"enabled": False, "url": "", "retention_days": 1}


# ---------------------------------------------------------------- 清理
def test_cleanup_by_days_keeps_recent_and_logs(tmp_path):
    svc, _ = _service(tmp_path, ("push", "push"))
    now = time.time()
    old = _seg(svc, "cam0", now - 11 * DAY)
    new = _seg(svc, "cam0", now - 2 * DAY)
    old1 = _seg(svc, "cam1", now - 3 * DAY)
    svc.update_settings({"cameras": {"cam1": {"retention_days": 2}}})       # 这一路只留 2 天
    assert svc.cleanup_expired(now) == 2
    assert not os.path.exists(old) and os.path.exists(new) and not os.path.exists(old1)
    reasons = [c["reason"] for c in svc.store.cleanups()]
    assert reasons == ["超过保留天数", "超过保留天数"]


def test_disk_guard_deletes_oldest_but_not_alert_clips(tmp_path, monkeypatch):
    svc, _ = _service(tmp_path)
    now = time.time()
    files = [_seg(svc, "cam0", now - 3600 + i * 60) for i in range(10)]
    clip = os.path.join(svc.root, "_alerts", "1.mp4")
    os.makedirs(os.path.dirname(clip))
    open(clip, "wb").write(b"x")
    svc.store.add_alert_clip(1, "cam0", now, now + 60, clip, 1, now + DAY)
    Usage = namedtuple("Usage", "total used free")
    state = {"used": 90}

    def fake_usage(_):
        remaining = sum(os.path.exists(f) for f in files)
        return Usage(100, 80 + remaining - 4 if remaining else 70, 0)   # 每删一片降 1%
    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    svc.guard_batch = 1
    ui = svc.bus.subscribe(TOPIC_UI)
    svc.guard_disk()
    assert sum(os.path.exists(f) for f in files) == 4 and not os.path.exists(files[0]) and os.path.exists(files[-1])
    assert os.path.exists(clip)
    assert svc.store.cleanups()[0]["reason"] == "磁盘空间不足"
    assert ui.get_nowait()["type"] == "storage_warning"


def test_disk_guard_pauses_at_stop_line(tmp_path, monkeypatch):
    svc, _ = _service(tmp_path)
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _: Usage(100, 97, 3))
    svc._started = True
    svc.guard_disk()
    assert svc.paused and svc.status("cam0")["state"] == "paused" and not svc.recorders
    monkeypatch.setattr(shutil, "disk_usage", lambda _: Usage(100, 50, 50))
    svc.guard_disk()
    assert not svc.paused and "cam0" in svc.recorders
    for r, _ in svc.recorders.values():
        r.stop()


# ---------------------------------------------------------------- 按帧录制
@needs_ffmpeg
def test_frame_recorder_writes_segment(tmp_path):
    from vva.video.frame_buffer import FrameBuffer
    buf = FrameBuffer(seconds=5, fps=5)
    got = []
    rec = FrameRecorder("cam0", buf, str(tmp_path / "out"), lambda *a: got.append(a), segment_s=60, fps=10)
    rec.start()
    for i in range(12):
        buf.add(Frame(image=np.full((90, 160, 3), i * 20, np.uint8), ts=time.time()))
        time.sleep(0.1)
    rec.stop()
    assert 1 <= len(got) <= 2                       # 正好跨过整分钟时会切成两片
    assert all(os.path.isfile(g[0]) and g[3] > 0 and g[4] == "mpeg4" and g[5] for g in got)
    assert 0.8 < sum(g[2] - g[1] for g in got) < 2.5


def test_stream_recorder_command():
    r = StreamRecorder("cam0", "rtsp://a:b@1.2.3.4/x", "/tmp/rec", lambda *a: None, segment_s=60)
    cmd = r.command("hevc")
    assert "-rtsp_transport" in cmd and "-use_wallclock_as_timestamps" in cmd and "hvc1" in cmd
    assert cmd[cmd.index("-segment_time") + 1] == "60" and cmd[-1].endswith("%Y%m%d-%H%M%S.mp4")
    assert "hvc1" not in r.command("h264")


# ---------------------------------------------------------------- 回放、告警片段
@needs_ffmpeg
def test_playback_clip_frame_and_alert_clip(tmp_path):
    async def run():
        svc, _ = _service(tmp_path)
        t0 = time.time() - 60
        _h264_segments(svc, "cam0", t0, n=3, dur=2)
        p = svc.playback.clip("cam0", t0 + 1, t0 + 4)                      # 跨两片，H.264 直接拼接
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                                   capture_output=True, text=True).stdout)
        assert 2.5 < dur < 4.5
        assert svc.playback.clip("cam0", t0 + 1, t0 + 4) == p                # 第二次用缓存
        assert svc.playback.frame("cam0", t0 + 3)[:2] == b"\xff\xd8"
        from vva.playback import NoRecording
        with pytest.raises(NoRecording):
            svc.playback.clip("cam0", t0 - 1000, t0 - 900)
        c = await svc.save_alert_clip(7, "cam0", t0 + 3, wait=False)
        assert c and os.path.isfile(c["path"]) and c["keep_until"] > time.time() + 179 * DAY
        assert svc.store.alert_clip_stats()["count"] == 1
    asyncio.run(run())


@needs_ffmpeg
def test_alert_event_triggers_clip_and_false_shortens_keep(tmp_path):
    async def run():
        svc, _ = _service(tmp_path, segment_s=0.1, alert_clip={"before_s": 1, "after_s": 1, "retention_days": 180})
        t0 = time.time() - 30
        _h264_segments(svc, "cam0", t0, n=3, dur=2)
        svc._started = True
        svc._tasks = [asyncio.create_task(svc._alert_loop()), asyncio.create_task(svc._ui_loop())]
        await asyncio.sleep(0)
        svc.bus.bind_loop(asyncio.get_running_loop())
        svc.bus.publish(TOPIC_ALERT, {"kind": "alert", "alert_id": 9, "camera": "", "ts": t0 + 3})
        for _ in range(100):
            await asyncio.sleep(0.1)
            if svc.store.alert_clip(9):
                break
        c = svc.store.alert_clip(9)
        assert c and c["camera"] == "cam0"                      # 没写摄像头：算第一路
        svc.bus.publish(TOPIC_UI, {"type": "alert_update", "id": 9, "alert": {"id": 9, "status": "false"}})
        await asyncio.sleep(0.1)
        assert svc.store.alert_clip(9)["keep_until"] < time.time() + 11 * DAY
        svc.bus.publish(TOPIC_UI, {"type": "alert_update", "deleted": [9]})
        await asyncio.sleep(0.1)
        assert svc.store.alert_clip(9) is None and not os.path.exists(c["path"])
        await svc.stop()
    asyncio.run(run())


def test_overview_and_status(tmp_path):
    svc, _ = _service(tmp_path, ("push", "rtsp://x/y"))
    now = time.time()
    _seg(svc, "cam0", now - 1800, size=5_000_000)
    o = svc.overview()
    assert o["recording_bytes"] == 5_000_000 and o["per_day_bytes"] > 5_000_000 and o["days_left"] is not None
    assert [c["type"] for c in o["cameras"]] == ["push", "stream"]
    assert o["cameras"][0]["status"]["state"] == "stopped"           # 还没启动
    svc.update_settings({"cameras": {"cam1": {"enabled": False}}})
    assert svc.status("cam1")["text"] == "未开启录像"


def test_recording_api(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from tests.fakes import FakeLLM
    from tests.test_channels import _app
    from vva.web.server import create_web_app
    app = _app(tmp_path, FakeLLM([]), web={"admin_token": "pw"}, recording={"dir": str(tmp_path / "rec")})
    with TestClient(create_web_app(app)) as c:
        h = {"X-Token": "pw"}
        o = c.get("/api/recording/overview").json()
        assert o["settings"]["retention_days"] == 10 and o["cameras"][0]["id"] == "cam0"
        assert c.put("/api/recording/settings", json={"retention_days": 5}).status_code == 401
        assert c.put("/api/recording/settings", headers=h, json={"clean_at": 80, "clean_to": 90}).status_code == 400
        s = c.put("/api/recording/settings", headers=h, json={"retention_days": 5, "cameras": {
            "cam0": {"enabled": True, "url": "rtsp://a:pw@1.2.3.4/main"}}}).json()
        assert s["retention_days"] == 5 and "******" in s["cameras"]["cam0"]["url"]
        c.put("/api/recording/settings", headers=h, json={"cameras": {"cam0": {"url": s["cameras"]["cam0"]["url"]}}})
        assert app.recording.settings()["cameras"]["cam0"]["url"] == "rtsp://a:pw@1.2.3.4/main"
        assert c.get("/api/recording/timeline?cam=cam0").json()["spans"] == []
        assert c.get(f"/api/playback/clip?cam=cam0&start={time.time() - 100}&end={time.time()}").status_code == 404
        assert c.get("/api/playback/clip?cam=nope&start=1&end=2").status_code == 404
        assert c.get("/recording").status_code == 200
