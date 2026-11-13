"""启动入口。

  python main.py -c config.yaml              启动服务，浏览器打开 http://localhost:8000
  python main.py -c config.yaml --browser    浏览器测试模式，访问 http://localhost:8000/browser
  python main.py -c config.yaml --no-audio   只用文字交互
  python main.py --list-devices              列出本机麦克风/扬声器设备
  python main.py --probe-cameras             探测本机可用的摄像头编号
"""
from __future__ import annotations

import argparse
import logging


def probe_cameras(max_index: int = 5) -> None:
    import cv2
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        ok, img = cap.read() if cap.isOpened() else (False, None)
        print(f"摄像头 {i}: {'可用 ' + str(img.shape[1]) + 'x' + str(img.shape[0]) if ok else '不可用'}")
        cap.release()


def main() -> None:
    p = argparse.ArgumentParser(description="实时语音视频分析智能体")
    p.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    p.add_argument("--no-audio", action="store_true", help="关闭语音，只用文字交互")
    p.add_argument("--list-devices", action="store_true", help="列出音频设备")
    p.add_argument("--probe-cameras", action="store_true", help="探测本机摄像头")
    p.add_argument("--browser", action="store_true",
                   help="浏览器测试模式：用网页调用电脑摄像头和麦克风，访问 /browser")
    p.add_argument("--port", type=int, default=0, help="覆盖配置中的端口")
    p.add_argument("--ssl-cert", default="", help="HTTPS 证书文件（远程用浏览器摄像头/麦克风时需要）")
    p.add_argument("--ssl-key", default="", help="HTTPS 私钥文件")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.list_devices:
        from vva.audio.local import list_devices
        print(list_devices())
        return
    if args.probe_cameras:
        probe_cameras()
        return

    import uvicorn

    from vva.app import App
    from vva.core.config import load_config
    from vva.web.server import create_web_app

    cfg = load_config(args.config)
    if args.browser:
        # 浏览器模式：凡是原本要在服务器本机打开的设备（local），都改由网页提供。
        # 已配置 RTSP 等视频源（stream/custom）时保留，网页上显示该视频并用浏览器麦克风对话。
        if cfg["video"]["type"] == "local":
            cfg["video"]["type"] = "push"
        for side in ("input", "output"):
            if cfg["audio"][side]["type"] == "local":
                cfg["audio"][side]["type"] = "push"
    app = App(cfg, enable_audio=False if args.no_audio else None)
    web = create_web_app(app)
    host, port = cfg["web"]["host"], int(cfg["web"]["port"])
    if args.port:
        port = args.port
    cert = args.ssl_cert or cfg["web"].get("ssl_certfile") or None
    key = args.ssl_key or cfg["web"].get("ssl_keyfile") or None
    if bool(cert) != bool(key):
        raise SystemExit("HTTPS 需要同时提供证书和私钥（--ssl-cert 与 --ssl-key）")
    scheme = "https" if cert else "http"
    page = "/browser" if args.browser else "/"
    log = logging.getLogger("main")
    log.info("服务地址 %s://<服务器IP>:%d%s", scheme, port, page)
    if args.browser and not cert:
        log.warning("浏览器只在 localhost 或 HTTPS 下允许使用摄像头/麦克风。"
                    "从其他电脑访问请用 HTTPS（scripts/gen_cert.sh）或 SSH 端口转发，详见 DEPLOY_LINUX.md")
    uvicorn.run(web, host=host, port=port, log_level="warning", ssl_certfile=cert, ssl_keyfile=key)


if __name__ == "__main__":
    main()
