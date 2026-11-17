"""视觉大模型压测：测 QPS（每秒完成多少次看图请求）和延迟，估算能带几路摄像头。

请求内容和后台观察一模一样（一张画面 + 观察提示词 + 3 条职责，输出 JSON），按并发数逐级加压：

  python scripts/bench_vlm.py -c config.yaml                        默认并发 1,2,4,8,16，每级 30 秒
  python scripts/bench_vlm.py -c config.yaml --image /tmp/snap.jpg  用真实画面（推荐）
  python scripts/bench_vlm.py -c config.yaml --mode look            测“提问看画面”（短回答）
  python scripts/bench_vlm.py -c config.yaml -n 2,4,6 -t 60         自定义并发和时长

取一张真实画面：curl -sk https://127.0.0.1:9000/api/snapshot.jpg -o /tmp/snap.jpg

注意：压测时 vva-app 如果还在跑，它的请求也会占模型，测出来会偏低；想测上限先 sudo systemctl stop vva-app。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from vva.agent import prompts  # noqa: E402
from vva.core.config import load_config  # noqa: E402
from vva.core.jsonutil import parse_json  # noqa: E402
from vva.models import clients  # noqa: E402
from vva.perception.imaging import decode_jpeg, encode_jpeg, resize_max_side  # noqa: E402

RULES = "- d1：画面中有人吸烟，或手里夹着点燃的香烟\n- d2：画面中的人低头玩手机\n- d3：画面中有人没有戴安全帽"


def test_image(path: str, side: int) -> bytes:
    if path:
        with open(path, "rb") as f:
            img = decode_jpeg(f.read())
    else:                                   # 没给图时画一张简单的场景
        rng = np.random.default_rng(0)
        img = np.zeros((720, 1280, 3), np.uint8)
        img[:] = (70, 66, 60)
        cv2.rectangle(img, (0, 500), (1280, 720), (60, 66, 72), -1)
        for _ in range(8):
            x, w, h = int(rng.integers(0, 1100)), int(rng.integers(80, 220)), int(rng.integers(120, 360))
            cv2.rectangle(img, (x, 500 - h), (x + w, 500), tuple(int(v) for v in rng.integers(70, 160, 3)), -1)
        for x in (300, 700):
            cv2.circle(img, (x, 400), 24, (150, 170, 190), -1)
            cv2.rectangle(img, (x - 28, 426), (x + 28, 560), (90, 110, 150), -1)
    return encode_jpeg(resize_max_side(img, side), 85)


def build(mode: str) -> tuple[str, str, int]:
    """(system, prompt, max_tokens)"""
    if mode == "look":
        return prompts.LOOK_SYSTEM, "画面里现在有几个人？分别在做什么？", 256
    prompt = prompts.OBSERVE_PROMPT.format(prev="", rules=prompts.OBSERVE_RULES_BLOCK.format(rules=RULES),
                                           scene_hint="\n场景说明：仓库内禁止吸烟\n")
    return prompts.OBSERVER_SYSTEM, prompt, 512


async def one(client, cfg, model, image_b64, system, prompt, max_tokens) -> tuple[float, int, bool]:
    t0 = time.perf_counter()
    resp = await client.chat.completions.create(
        model=model, max_tokens=max_tokens, temperature=0.1, **clients._extra(cfg),
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": [{"type": "image_url", "image_url": {"url": image_b64}},
                                               {"type": "text", "text": prompt}]}])
    dt = time.perf_counter() - t0
    text = resp.choices[0].message.content or ""
    out_tokens = resp.usage.completion_tokens if resp.usage else 0
    return dt, out_tokens, bool(text.strip())


async def level(conc: int, seconds: float, args, client, cfg, image_b64, system, prompt, max_tokens) -> dict:
    lat: list[float] = []
    tokens = errors = empty = 0
    stop = time.perf_counter() + seconds

    async def worker():
        nonlocal tokens, errors, empty
        while time.perf_counter() < stop:
            try:
                dt, n, ok = await one(client, cfg, cfg["model"], image_b64, system, prompt, max_tokens)
                lat.append(dt)
                tokens += n
                empty += 0 if ok else 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"    请求失败：{e}")
                await asyncio.sleep(0.5)

    t0 = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(conc)))
    wall = time.perf_counter() - t0
    lat.sort()
    p = (lambda q: lat[min(len(lat) - 1, int(len(lat) * q))]) if lat else (lambda q: 0.0)
    return {"conc": conc, "done": len(lat), "errors": errors, "empty": empty, "qps": len(lat) / wall if wall else 0,
            "avg": statistics.mean(lat) if lat else 0, "p50": p(0.5), "p95": p(0.95), "tok_s": tokens / wall if wall else 0}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-n", "--concurrency", default="1,2,4,8,16", help="逐级的并发数，逗号分开")
    ap.add_argument("-t", "--seconds", type=float, default=30, help="每级压多少秒")
    ap.add_argument("--image", default="", help="测试用的画面（JPEG），不填则用画出来的场景")
    ap.add_argument("--mode", choices=["observe", "look"], default="observe", help="observe 后台观察（默认）；look 提问看画面")
    args = ap.parse_args()

    cfg_all = load_config(args.config)
    cfg = cfg_all["models"]["vlm"]
    side = cfg_all["observer"].get("image_side", 1024) if args.mode == "observe" else cfg_all["agent"].get("look_max_side", 1280)
    import base64
    jpeg = test_image(args.image, int(side))
    image_b64 = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    system, prompt, max_tokens = build(args.mode)
    client = clients._make_client(cfg)
    h, w = decode_jpeg(jpeg).shape[:2]
    print(f"模型：{cfg['model']}  地址：{cfg['base_url']}")
    print(f"请求：{'后台观察（3 条职责，输出 JSON）' if args.mode == 'observe' else '提问看画面'}，图片 {w}×{h}，每级 {args.seconds:.0f} 秒\n")

    print("预热…", end="", flush=True)
    dt, n, ok = await one(client, cfg, cfg["model"], image_b64, system, prompt, max_tokens)
    print(f" 单次 {dt:.2f} 秒，输出 {n} token" + ("" if ok else "（回答为空，检查模型）"))
    if args.mode == "observe" and not isinstance(parse_json((await client.chat.completions.create(
            model=cfg["model"], max_tokens=max_tokens, **clients._extra(cfg),
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": [{"type": "image_url", "image_url": {"url": image_b64}},
                                                   {"type": "text", "text": prompt}]}])).choices[0].message.content), dict):
        print("  注意：返回的不是 JSON，后台观察会解析失败")
    print()

    rows = []
    print(f"{'并发':>4} {'完成':>6} {'失败':>4} {'QPS':>7} {'平均延迟':>8} {'P50':>7} {'P95':>7} {'输出tok/s':>10}")
    for c in [int(x) for x in args.concurrency.split(",") if x.strip()]:
        r = await level(c, args.seconds, args, client, cfg, image_b64, system, prompt, max_tokens)
        rows.append(r)
        print(f"{r['conc']:>4} {r['done']:>6} {r['errors']:>4} {r['qps']:>7.2f} {r['avg']:>7.2f}s {r['p50']:>6.2f}s "
              f"{r['p95']:>6.2f}s {r['tok_s']:>10.0f}")

    if args.mode == "observe":
        interval = float(cfg_all["observer"].get("rule_check_interval", 8))
        ok_rows = [r for r in rows if r["done"] and not r["errors"] and r["p95"] <= interval / 2]
        print(f"\n估算（后台观察有职责时每路每 {interval:.0f} 秒看一次，要求 P95 延迟不超过 {interval / 2:.0f} 秒）：")
        if ok_rows:
            best = max(ok_rows, key=lambda r: r["qps"])
            cams = int(best["qps"] * interval * 0.7)        # 留 30% 给提问、复核
            print(f"  最多约 {best['qps']:.2f} QPS（并发 {best['conc']}），留出 30% 给用户提问和告警复核，"
                  f"大约能带 {cams} 路摄像头")
            print(f"  vision.max_concurrency 建议设为 {best['conc']}")
        else:
            print("  没有一级满足延迟要求：检查模型服务，或调大 observer.rule_check_interval")


if __name__ == "__main__":
    asyncio.run(main())
