"""跨进程音频接入示例：把本机麦克风推给服务，并播放服务返回的语音。

服务端配置 audio.input.type: push 和 audio.output.type: push，然后运行：
    pip install websockets sounddevice
    python examples/push_audio_client.py --server ws://127.0.0.1:8000

协议（/api/ingest/audio，WebSocket）：
  1. 连接后服务端先发一条 JSON：{"type":"config","input_sample_rate":16000,"output_sample_rate":24000,...}
  2. 客户端发送二进制消息：PCM 16-bit 小端单声道，采样率 = input_sample_rate，建议每包 20~40ms
  3. 服务端发送二进制消息：要播放的 PCM，采样率 = output_sample_rate
  4. 服务端发送 JSON {"type":"clear"}：立即停止播放并清空缓存（用户打断时）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import queue

import sounddevice as sd
import websockets


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="ws://127.0.0.1:8000")
    p.add_argument("--token", default="")
    args = p.parse_args()
    url = args.server.rstrip("/") + "/api/ingest/audio" + (f"?token={args.token}" if args.token else "")

    async with websockets.connect(url, max_size=None) as ws:
        cfg = json.loads(await ws.recv())
        in_sr, out_sr = cfg.get("input_sample_rate") or 16000, cfg.get("output_sample_rate") or 24000
        print("已连接：", cfg)
        loop = asyncio.get_running_loop()
        mic_q: asyncio.Queue[bytes] = asyncio.Queue()
        play_q: queue.Queue[bytes] = queue.Queue()
        pending = bytearray()

        def mic_cb(indata, frames, t, status):
            loop.call_soon_threadsafe(mic_q.put_nowait, bytes(indata))

        def spk_cb(outdata, frames, t, status):
            need = frames * 2
            while len(pending) < need and not play_q.empty():
                pending.extend(play_q.get_nowait())
            chunk = bytes(pending[:need])
            del pending[:need]
            outdata[:] = chunk + b"\x00" * (need - len(chunk))

        def clear_playback():
            while not play_q.empty():
                play_q.get_nowait()
            pending.clear()

        async def uplink():
            while True:
                await ws.send(await mic_q.get())

        async def downlink():
            async for msg in ws:
                if isinstance(msg, bytes):
                    play_q.put(msg)
                elif json.loads(msg).get("type") == "clear":
                    clear_playback()

        with sd.RawInputStream(samplerate=in_sr, channels=1, dtype="int16", blocksize=int(in_sr * 0.03), callback=mic_cb), \
             sd.RawOutputStream(samplerate=out_sr, channels=1, dtype="int16", blocksize=int(out_sr * 0.02), callback=spk_cb):
            await asyncio.gather(uplink(), downlink())


if __name__ == "__main__":
    asyncio.run(main())
