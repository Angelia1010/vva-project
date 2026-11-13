"""跨进程推帧示例：从任意来源取画面，通过 HTTP 推送给智能体服务。

服务端配置 video.type: push，然后运行：
    python examples/push_frames_client.py --source 0                      # 本机摄像头
    python examples/push_frames_client.py --source rtsp://...  --fps 5    # 其他流
其他语言（C++/Java/Go）同理：POST JPEG 字节到 /api/ingest/frame 即可。
"""
from __future__ import annotations

import argparse
import time
import urllib.request

import cv2


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--source", default="0", help="摄像头编号或视频地址")
    p.add_argument("--server", default="http://127.0.0.1:8000")
    p.add_argument("--fps", type=float, default=5)
    p.add_argument("--token", default="")
    args = p.parse_args()

    src = int(args.source) if args.source.isdigit() else args.source
    cap = cv2.VideoCapture(src)
    url = args.server.rstrip("/") + "/api/ingest/frame"
    gap = 1.0 / args.fps
    while True:
        t0 = time.time()
        ok, img = cap.read()
        if not ok:
            print("读取失败，1 秒后重试")
            time.sleep(1)
            cap.release()
            cap = cv2.VideoCapture(src)
            continue
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        req = urllib.request.Request(url, data=buf.tobytes(), method="POST", headers={
            "Content-Type": "image/jpeg", "X-Timestamp": str(time.time()), "X-Token": args.token})
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:
            print("推送失败：", e)
        time.sleep(max(0.0, gap - (time.time() - t0)))


if __name__ == "__main__":
    main()
