"""OpenAI 格式的模型客户端。四类模型各自独立配置 base_url / api_key / model：

- ChatModel       /v1/chat/completions（流式 + 函数调用），智能体使用
- VisionModel     /v1/chat/completions（image_url 传图），看图使用
- SpeechToText    /v1/audio/transcriptions，语音识别
- TextToSpeech    /v1/audio/speech，语音合成（流式 pcm 或 wav）

只要你部署的服务兼容这些接口（vLLM、SGLang、各云厂商兼容模式、自写封装等）即可接入。
其他模块只依赖这里的方法签名，方便替换或在测试中用假实现代替。
"""
from __future__ import annotations

import base64
import logging
from typing import Any, AsyncIterator

from vva.audio.utils import pcm_to_wav, resample_pcm16, wav_to_pcm

log = logging.getLogger(__name__)


def _make_client(cfg: dict[str, Any]):
    try:
        from openai import AsyncOpenAI
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("需要安装 openai：pip install openai") from e
    if not cfg.get("base_url") or not cfg.get("model"):
        raise ValueError(f"模型配置缺少 base_url 或 model: {cfg}")
    return AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg.get("api_key") or "EMPTY",
                       timeout=float(cfg.get("timeout") or 60))


def _extra(cfg: dict[str, Any]) -> dict[str, Any]:
    return {"extra_body": cfg["extra_body"]} if cfg.get("extra_body") else {}


class ChatModel:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.model = cfg["model"]
        self.client = _make_client(cfg)
        self._required_ok = True     # 服务端是否支持 tool_choice="required"

    async def stream(self, messages: list[dict], tools: list[dict] | None = None,
                     tool_choice: str = "auto") -> AsyncIterator[tuple[str, Any]]:
        """产出 ("text", 增量文本)；若模型要调用工具，最后产出 ("tool_calls", [{id,name,arguments}])。

        tool_choice="required" 强制模型本轮调用工具；服务端不支持时自动退回 "auto"。
        """
        if tool_choice == "required" and tools and self._required_ok:
            try:
                async for item in self._stream(messages, tools, "required"):
                    yield item
                return
            except Exception as e:  # 不同服务端报错信息不一，统一退回 auto，本进程内不再尝试
                log.warning("tool_choice=required 调用失败，之后改用 auto：%s", e)
                self._required_ok = False
        async for item in self._stream(messages, tools, "auto"):
            yield item

    async def _stream(self, messages: list[dict], tools: list[dict] | None,
                      tool_choice: str) -> AsyncIterator[tuple[str, Any]]:
        kwargs: dict[str, Any] = dict(model=self.model, messages=messages,
                                      temperature=self.cfg.get("temperature", 0.3), **_extra(self.cfg))
        if self.cfg.get("max_tokens"):
            kwargs["max_tokens"] = int(self.cfg["max_tokens"])
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        if not self.cfg.get("stream", True):
            resp = await self.client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            if msg.content:
                yield "text", msg.content
            if msg.tool_calls:
                yield "tool_calls", [{"id": c.id, "name": c.function.name,
                                      "arguments": c.function.arguments or "{}"} for c in msg.tool_calls]
            return
        resp = await self.client.chat.completions.create(stream=True, **kwargs)
        calls: dict[int, dict[str, str]] = {}
        async for chunk in resp:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                yield "text", delta.content
            for tc in getattr(delta, "tool_calls", None) or []:
                idx = tc.index if tc.index is not None else len(calls)
                slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function is not None:
                    if tc.function.name:
                        slot["name"] = tc.function.name if not slot["name"] else slot["name"] + (
                            "" if slot["name"] == tc.function.name else tc.function.name)
                    if tc.function.arguments:
                        slot["arguments"] += tc.function.arguments
        if calls:
            yield "tool_calls", [calls[i] for i in sorted(calls)]


class VisionModel:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.model = cfg["model"]
        self.bbox_format = cfg.get("bbox_format", "0-1000")
        self.max_image_side = int(cfg.get("max_image_side", 1280))
        self.client = _make_client(cfg)

    async def ask(self, images: list[bytes], prompt: str, system: str | None = None,
                  json_mode: bool = False, max_tokens: int | None = None) -> str:
        content: list[dict[str, Any]] = [
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}}
            for img in images
        ]
        content.append({"type": "text", "text": prompt})
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})
        kwargs: dict[str, Any] = dict(model=self.model, messages=messages,
                                      temperature=self.cfg.get("temperature", 0.1),
                                      max_tokens=max_tokens or self.cfg.get("max_tokens", 1024),
                                      **_extra(self.cfg))
        if json_mode and self.cfg.get("json_mode"):
            kwargs["response_format"] = {"type": "json_object"}
        resp = await self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""


class EmbeddingModel:
    """/v1/embeddings，知识库向量检索使用（可选）。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.model = cfg["model"]
        self.batch = int(cfg.get("batch_size") or 32)
        self.client = _make_client(cfg)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            resp = await self.client.embeddings.create(model=self.model, input=texts[i:i + self.batch],
                                                       **_extra(self.cfg))
            out += [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
        return out


class SpeechToText:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.model = cfg["model"]
        self.client = _make_client(cfg)

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        kwargs: dict[str, Any] = dict(model=self.model,
                                      file=("speech.wav", pcm_to_wav(pcm, sample_rate), "audio/wav"),
                                      **_extra(self.cfg))
        if self.cfg.get("language"):
            kwargs["language"] = self.cfg["language"]
        if self.cfg.get("prompt"):
            kwargs["prompt"] = self.cfg["prompt"]  # 可放热词、专有名词
        resp = await self.client.audio.transcriptions.create(**kwargs)
        text = resp if isinstance(resp, str) else getattr(resp, "text", "")
        return (text or "").strip()


class TextToSpeech:
    """语音合成。

    配置项（models.tts）：
      response_format  pcm（推荐，可流式）| wav
      sample_rate      本地播放使用的采样率；服务端返回其他采样率时会自动重采样
      stream           是否请求服务端流式输出（vLLM-Omni 需要 stream=true 才会边合成边返回）
      stream_format    流式格式；vLLM-Omni 默认是 SSE 事件，这里固定要 audio（原始音频字节）
      language         如 Chinese / English / Auto（Qwen3-TTS 支持）
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.model = cfg["model"]
        self.sample_rate = int(cfg.get("sample_rate", 24000))
        self.fmt = (cfg.get("response_format") or "pcm").lower()
        self.streaming = bool(cfg.get("stream", False)) and self.fmt == "pcm"
        self.client = _make_client(cfg)

    def _request_kwargs(self, text: str) -> dict[str, Any]:
        body: dict[str, Any] = dict(self.cfg.get("extra_body") or {})
        if self.streaming:
            body.setdefault("stream", True)
            body.setdefault("stream_format", self.cfg.get("stream_format") or "audio")
        if self.cfg.get("language"):
            body.setdefault("language", self.cfg["language"])
        kwargs: dict[str, Any] = dict(model=self.model, voice=self.cfg.get("voice") or "alloy",
                                      input=text, response_format=self.fmt)
        if body:
            kwargs["extra_body"] = body
        if self.cfg.get("speed") and not self.streaming:  # 流式时服务端要求 speed=1.0
            kwargs["speed"] = self.cfg["speed"]
        return kwargs

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """产出 PCM16 单声道片段，采样率为 self.sample_rate。"""
        async with self.client.audio.speech.with_streaming_response.create(**self._request_kwargs(text)) as resp:
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/event-stream" in ctype:
                raise RuntimeError("TTS 服务返回了 SSE 事件流，请在配置中设置 tts.stream_format: audio")
            hdr_sr = resp.headers.get("x-audio-sample-rate")
            if self.fmt == "pcm" and "wav" not in ctype:
                src_sr = int(hdr_sr) if hdr_sr and hdr_sr.isdigit() else self.sample_rate
                rest = b""
                async for chunk in resp.iter_bytes(4800):
                    data = rest + chunk
                    even = len(data) // 2 * 2
                    if even:
                        yield resample_pcm16(data[:even], src_sr, self.sample_rate)
                    rest = data[even:]
            else:  # wav，或服务端忽略了 pcm 要求而返回 wav
                pcm, sr = wav_to_pcm(await resp.read())
                yield resample_pcm16(pcm, sr, self.sample_rate)
