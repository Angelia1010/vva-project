"""模型服务自检：逐项验证项目依赖的接口都能正常工作。

  python scripts/check_models.py -c config.yaml           全部检查
  python scripts/check_models.py -c config.yaml --only vlm
  python scripts/check_models.py -c config.yaml --voices  列出 TTS 可用音色

检查内容：
  1. VLM 看图问答
  2. VLM 函数调用（智能体必需）
  3. VLM 定位框坐标格式（验证 bbox_format 是否配对）
  4. TTS 流式合成（首包延迟、采样率），保存到 check_output/tts.wav
  5. ASR 识别上一步合成的语音（来回校验）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from vva.agent import prompts  # noqa: E402
from vva.audio.utils import pcm_to_wav, resample_pcm16  # noqa: E402
from vva.core.config import load_config  # noqa: E402
from vva.core.jsonutil import parse_json  # noqa: E402
from vva.models import clients  # noqa: E402
from vva.perception.imaging import box_to_normalized, encode_jpeg  # noqa: E402

OUT = os.path.join(ROOT, "check_output")
OK, FAIL, WARN = "\033[32m通过\033[0m", "\033[31m失败\033[0m", "\033[33m注意\033[0m"
results: list[tuple[str, bool]] = []


def report(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
    results.append((name, ok))
    print(f"[{WARN if (ok and warn) else (OK if ok else FAIL)}] {name}  {detail}")


def test_image() -> tuple[bytes, list[float]]:
    """白底图：左侧大字 “2580”，右上一个红色圆形。返回 JPEG 和圆的真实归一化框。"""
    w, h = 1280, 720
    img = np.full((h, w, 3), 255, np.uint8)
    cv2.putText(img, "2580", (90, 420), cv2.FONT_HERSHEY_SIMPLEX, 6, (20, 20, 20), 18)
    cx, cy, r = 980, 220, 90
    cv2.circle(img, (cx, cy), r, (0, 0, 230), -1)
    return encode_jpeg(img, 92), [(cx - r) / w, (cy - r) / h, (cx + r) / w, (cy + r) / h]


def iou(a: list[float], b: list[float]) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / area if area > 0 else 0.0


async def check_vlm(cfg: dict) -> None:
    print("\n== 看图模型 ==", cfg["models"]["vlm"]["base_url"], cfg["models"]["vlm"]["model"])
    vlm = clients.VisionModel(cfg["models"]["vlm"])
    jpeg, truth = test_image()

    t0 = time.time()
    try:
        ans = await vlm.ask([jpeg], "图片左侧的大号数字是多少？只回答数字。")
        report("看图问答", "2580" in ans.replace(" ", ""), f"回答：{ans.strip()[:40]}  耗时 {time.time() - t0:.1f}s")
    except Exception as e:
        report("看图问答", False, f"请求失败：{e}")
        return

    llm = clients.ChatModel(cfg["models"]["agent_llm"])
    tools = [{"type": "function", "function": {
        "name": "look_now", "description": "看摄像头当前画面并回答问题",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}}]
    t0 = time.time()
    try:
        calls, text = [], ""
        async for kind, val in llm.stream([{"role": "user", "content": "帮我看看摄像头画面里有什么？"}], tools=tools):
            if kind == "tool_calls":
                calls = val
            else:
                text += val
        ok = bool(calls) and calls[0]["name"] == "look_now"
        detail = f"调用：{calls[0]['name']}({calls[0]['arguments']})" if calls else f"未调用工具，回答：{text[:40]}"
        report("函数调用", ok, f"{detail}  耗时 {time.time() - t0:.1f}s")
        if not ok:
            print("    → 请确认 vLLM 启动参数含 --enable-auto-tool-choice 和正确的 --tool-call-parser")
    except Exception as e:
        report("函数调用", False, f"请求失败：{e}")

    fmt = vlm.bbox_format
    try:
        prompt = prompts.LOCATE_PROMPT.format(target="红色圆形", coord_rule=prompts.COORD_RULES.get(fmt))
        data = parse_json(await vlm.ask([jpeg], prompt, json_mode=True))
        raw = (data.get("objects") or [{}])[0].get("box_2d") if isinstance(data, dict) else None
        box = box_to_normalized(raw, fmt, 1280, 720) if raw else None
        score = iou(box, truth) if box else 0.0
        report(f"定位坐标（bbox_format={fmt}）", score >= 0.4,
               f"模型输出 {raw} → 归一化 {[round(v, 2) for v in box] if box else None}，真实 {[round(v, 2) for v in truth]}，IoU={score:.2f}")
        if raw and score < 0.4:
            for alt in ("0-1000", "pixel", "0-1"):
                b = box_to_normalized(raw, alt, 1280, 720)
                if b and iou(b, truth) >= 0.4:
                    print(f"    → 按 {alt} 解释时位置正确，请把 models.vlm.bbox_format 改为 \"{alt}\"")
    except Exception as e:
        report("定位坐标", False, f"请求失败：{e}")


async def check_tts(cfg: dict) -> bytes | None:
    t = cfg["models"]["tts"]
    print("\n== 语音合成 ==", t["base_url"], t["model"], "音色", t.get("voice"))
    tts = clients.TextToSpeech(t)
    text = "你好，这是语音合成测试，今天的温度是二十五度。"
    t0, first, chunks = time.time(), None, []
    try:
        async for pcm in tts.stream(text):
            first = first or time.time() - t0
            chunks.append(pcm)
    except Exception as e:
        report("流式合成", False, f"请求失败：{e}")
        return None
    pcm = b"".join(chunks)
    dur = len(pcm) / 2 / tts.sample_rate
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "tts.wav"), "wb") as f:
        f.write(pcm_to_wav(pcm, tts.sample_rate))
    ok = 1.5 < dur < 15
    report("流式合成", ok, f"首包 {first:.2f}s，总耗时 {time.time() - t0:.2f}s，音频 {dur:.1f}s，"
                       f"分 {len(chunks)} 包（1 包说明不是流式）→ check_output/tts.wav", warn=len(chunks) <= 1)
    return pcm if ok else None


async def check_asr(cfg: dict, tts_pcm: bytes | None, tts_sr: int) -> None:
    a = cfg["models"]["asr"]
    print("\n== 语音识别 ==", a["base_url"], a["model"])
    if tts_pcm is None:
        print("    没有可用的测试语音（TTS 检查未通过），改用 check_output/asr_input.wav（如有）")
        path = os.path.join(OUT, "asr_input.wav")
        if not os.path.exists(path):
            report("语音识别", False, "缺少测试音频")
            return
        from vva.audio.utils import wav_to_pcm
        pcm, sr = wav_to_pcm(open(path, "rb").read())
        tts_pcm, tts_sr = pcm, sr
    pcm16k = resample_pcm16(tts_pcm, tts_sr, 16000)
    asr = clients.SpeechToText(a)
    t0 = time.time()
    try:
        text = await asr.transcribe(pcm16k, 16000)
        report("语音识别", "语音合成" in text or "测试" in text, f"识别：{text}  耗时 {time.time() - t0:.2f}s")
    except Exception as e:
        report("语音识别", False, f"请求失败：{e}")


def list_voices(cfg: dict) -> None:
    t = cfg["models"]["tts"]
    url = t["base_url"].rstrip("/") + "/audio/voices"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            print(json.dumps(json.loads(r.read()), ensure_ascii=False, indent=2))
    except Exception as e:
        print("获取音色失败：", e)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--only", choices=["vlm", "tts", "asr"])
    p.add_argument("--voices", action="store_true")
    args = p.parse_args()
    cfg = load_config(os.path.join(ROOT, args.config) if not os.path.isabs(args.config) else args.config)
    if args.voices:
        list_voices(cfg)
        return
    if args.only in (None, "vlm"):
        await check_vlm(cfg)
    pcm = None
    if args.only in (None, "tts", "asr"):
        pcm = await check_tts(cfg) if args.only != "asr" else None
    if args.only in (None, "asr"):
        await check_asr(cfg, pcm, int(cfg["models"]["tts"].get("sample_rate", 24000)))
    print()
    failed = [n for n, ok in results if not ok]
    print("全部通过 ✅" if not failed else f"未通过：{'、'.join(failed)}（排查方法见 DEPLOY_LINUX.md 常见问题）")


if __name__ == "__main__":
    asyncio.run(main())
