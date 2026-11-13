"""网页服务：实时画面（MJPEG）、对话与事件（WebSocket）、外部推流接入接口。

接口一览：
  GET  /                    测试页面
  GET  /video.mjpg          实时画面
  GET  /api/snapshot.jpg    当前画面截图
  GET  /api/status          运行状态
  GET  /api/timeline        画面时间线 ?minutes=10
  POST /api/chat            文字提问 {"text": "..."}（不等待回答，回答通过 /ws 推送）
  WS   /ws                  前端事件通道
  POST /api/ingest/frame    推送一帧 JPEG（video.type=push 时可用）
  WS   /api/ingest/audio    双向音频（audio.input/output.type=push 时可用）
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from vva.app import WEB_SESSION, App
from vva.core.bus import TOPIC_UI

log = logging.getLogger(__name__)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_web_app(app: App) -> FastAPI:
    web_cfg = app.cfg["web"]
    token = web_cfg.get("ingest_token") or ""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await app.start()
        yield
        await app.stop()

    api = FastAPI(title="实时语音视频分析智能体", lifespan=lifespan)

    def _check_token(provided: str | None) -> None:
        if token and provided != token:
            raise HTTPException(status_code=401, detail="ingest token 错误")

    # ------------------------------------------------------------ 页面与画面
    @api.get("/")
    async def index() -> FileResponse:
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    @api.get("/browser")
    async def browser_page() -> FileResponse:
        """浏览器测试页：用浏览器的摄像头和麦克风，需以 --browser 启动（推送模式）。"""
        return FileResponse(os.path.join(STATIC_DIR, "browser.html"))

    @api.get("/api/capabilities")
    async def capabilities() -> JSONResponse:
        from vva.audio.push import CallbackAudioOutput, PushAudioInput
        from vva.video.push_source import PushVideoSource
        return JSONResponse({
            "video_push": isinstance(app.video, PushVideoSource),
            "audio_in_push": isinstance(app.audio_in, PushAudioInput),
            "audio_out_push": isinstance(app.audio_out, CallbackAudioOutput),
            "voice": app.voice is not None,
            "voice_mode": app.voice.mode if app.voice else None,
            "token_required": bool(token),
        })

    @api.get("/video.mjpg")
    async def video_stream(request: Request) -> StreamingResponse:
        interval = 1.0 / max(1, int(web_cfg.get("mjpeg_fps", 10)))

        async def gen():
            last_ts = None
            while not await request.is_disconnected():
                item = app.buffer.preview_jpeg()
                if item and item[0] != last_ts:
                    last_ts, data = item
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                await asyncio.sleep(interval)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    @api.get("/api/snapshot.jpg")
    async def snapshot() -> Response:
        bf = app.buffer.latest_snapshot()
        if bf is None:
            raise HTTPException(404, "暂无画面")
        return Response(bf.jpeg, media_type="image/jpeg")

    @api.get("/api/status")
    async def status() -> JSONResponse:
        return JSONResponse(app.status())

    @api.get("/api/timeline")
    async def timeline(minutes: float = 10) -> JSONResponse:
        return JSONResponse([e.to_dict() for e in app.timeline.query(minutes)])

    @api.post("/api/chat")
    async def chat(body: dict) -> JSONResponse:
        await app.handle_user_text(str(body.get("text", "")), "api")
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------ 前端事件通道
    @api.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        q = app.bus.subscribe(TOPIC_UI)
        await sock.send_json({"type": "hello", "status": app.status(),
                              "timeline": [e.to_dict() for e in app.timeline.query(30)]})

        async def sender():
            while True:
                ev = await q.get()
                if ev.get("session") in (None, WEB_SESSION):   # 只推送网页会话和全局事件（时间线、告警等）
                    await sock.send_json(ev)

        send_task = asyncio.create_task(sender())
        try:
            while True:
                msg = await sock.receive_json()
                t = msg.get("type")
                if t == "chat":
                    await app.handle_user_text(str(msg.get("text", "")), "text")
                elif t == "interrupt":
                    await app.interrupt()
                elif t == "ptt" and app.voice:      # 按住说话（voice.mode=push_to_talk）
                    if msg.get("state") == "down":
                        app.voice.ptt_press()
                    else:
                        app.voice.ptt_release()
                elif t == "reset":
                    await app.interrupt()
                    app.reset_session(WEB_SESSION)
                    await sock.send_json({"type": "reset_ok"})
        except (WebSocketDisconnect, RuntimeError):
            pass
        except json.JSONDecodeError:
            pass
        finally:
            send_task.cancel()
            app.bus.unsubscribe(TOPIC_UI, q)

    # ------------------------------------------------------------ 外部视频接入
    from vva.video.push_source import PushVideoSource
    if isinstance(app.video, PushVideoSource):
        @api.post("/api/ingest/frame")
        async def ingest_frame(request: Request) -> JSONResponse:
            _check_token(request.headers.get("X-Token"))
            data = await request.body()
            ts_header = request.headers.get("X-Timestamp")
            ts = float(ts_header) if ts_header else time.time()
            ok = await asyncio.to_thread(app.video.push_jpeg, data, ts)
            if not ok:
                raise HTTPException(400, "JPEG 解码失败")
            return JSONResponse({"ok": True})

    # ------------------------------------------------------------ 外部音频接入
    from vva.audio.push import CallbackAudioOutput, PushAudioInput
    push_in = isinstance(app.audio_in, PushAudioInput)
    push_out = isinstance(app.audio_out, CallbackAudioOutput)
    if push_in or push_out:
        @api.websocket("/api/ingest/audio")
        async def ingest_audio(sock: WebSocket) -> None:
            if token and sock.query_params.get("token") != token:
                await sock.close(code=4401)
                return
            await sock.accept()
            await sock.send_json({"type": "config",
                                  "input_sample_rate": app.audio_in.sample_rate if push_in else None,
                                  "output_sample_rate": app.audio_out.sample_rate if push_out else None,
                                  "format": "pcm_s16le_mono"})
            out_q: asyncio.Queue = asyncio.Queue()

            def sink(pcm: bytes) -> None:
                out_q.put_nowait(pcm)

            def on_clear() -> None:
                while not out_q.empty():
                    out_q.get_nowait()
                out_q.put_nowait({"type": "clear"})

            if push_out:
                app.audio_out.add_sink(sink)
                app.audio_out.add_clear_listener(on_clear)

            async def sender():
                while True:
                    item = await out_q.get()
                    if isinstance(item, bytes):
                        await sock.send_bytes(item)
                    else:
                        await sock.send_json(item)

            send_task = asyncio.create_task(sender())
            try:
                while True:
                    msg = await sock.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("bytes") and push_in:
                        app.audio_in.push_pcm(msg["bytes"])
            except WebSocketDisconnect:
                pass
            finally:
                send_task.cancel()
                if push_out:
                    app.audio_out.remove_sink(sink)
                    app.audio_out.remove_clear_listener(on_clear)
                with contextlib.suppress(Exception):
                    await sock.close()

    # ------------------------------------------------------------ 场景包管理
    if app.scene:
        from vva.web.scene_api import register_scene_routes
        register_scene_routes(api, app.scene, app.snapshot, web_cfg.get("admin_token") or "", STATIC_DIR)

    # ------------------------------------------------------------ 知识库管理
    if app.knowledge:
        from vva.web.knowledge_api import register_knowledge_routes
        register_knowledge_routes(api, app.knowledge, web_cfg.get("admin_token") or "", STATIC_DIR)

    return api
