"""OpenAI 格式客户端测试：用假的 SDK 对象验证请求参数和响应解析，不需要 openai 包和模型服务。"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace as NS

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vva.audio.utils import pcm_to_wav  # noqa: E402
from vva.models import clients  # noqa: E402


class _Stream:
    def __init__(self, items):
        self.items = items

    def __aiter__(self):
        async def g():
            for x in self.items:
                yield x
        return g()


class _SpeechResp:
    def __init__(self, headers, chunks):
        self.headers, self.chunks = headers, chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def iter_bytes(self, n):
        for c in self.chunks:
            yield c

    async def read(self):
        return b"".join(self.chunks)


@pytest.fixture
def fake(monkeypatch):
    state = {"captured": {}, "speech": None, "chat": None}

    async def chat_create(**kw):
        state["captured"] = kw
        return state["chat"](kw)

    def speech_create(**kw):
        state["captured"] = kw
        return _SpeechResp(*state["speech"])

    client = NS(chat=NS(completions=NS(create=chat_create)),
                audio=NS(speech=NS(with_streaming_response=NS(create=speech_create))))
    monkeypatch.setattr(clients, "_make_client", lambda cfg: client)
    return state


def _chunk(content=None, tcs=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tcs))])


def _tc(i, id=None, name=None, args=None):
    return NS(index=i, id=id, function=NS(name=name, arguments=args))


def test_chat_stream_tool_calls(fake):
    fake["chat"] = lambda kw: _Stream([
        _chunk("我"), _chunk(None, [_tc(0, "a", "look_now", '{"que')]),
        _chunk(None, [_tc(0, None, None, 'stion": "读数"}')]),
        _chunk(None, [_tc(1, "b", "locate", '{"target":"表"}')]), NS(choices=[])])
    m = clients.ChatModel({"model": "x", "base_url": "u"})

    async def run():
        return [x async for x in m.stream([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])]
    out = asyncio.run(run())
    assert out[0] == ("text", "我")
    assert out[-1][1] == [{"id": "a", "name": "look_now", "arguments": '{"question": "读数"}'},
                          {"id": "b", "name": "locate", "arguments": '{"target":"表"}'}]
    assert fake["captured"]["tool_choice"] == "auto"


def test_chat_required_falls_back_to_auto(fake):
    seen = []

    def chat(kw):
        seen.append(kw["tool_choice"])
        if kw["tool_choice"] == "required":
            raise ValueError("400 bad request")
        return _Stream([_chunk(None, [_tc(0, "a", "look_now", "{}")])])
    fake["chat"] = chat
    m = clients.ChatModel({"model": "x", "base_url": "u"})

    async def run():
        return [x async for x in m.stream([{"role": "user", "content": "hi"}], tools=[{"type": "function"}],
                                          tool_choice="required")]
    assert asyncio.run(run())[-1][0] == "tool_calls"
    assert asyncio.run(run())[-1][0] == "tool_calls"
    assert seen == ["required", "auto", "auto"]          # 不支持时只试一次，之后直接用 auto


def test_vision_request(fake):
    fake["chat"] = lambda kw: NS(choices=[NS(message=NS(content="答案", tool_calls=None))])
    v = clients.VisionModel({"model": "vl", "base_url": "u", "json_mode": True})
    assert asyncio.run(v.ask([b"\xff\xd8"], "问题", system="s", json_mode=True)) == "答案"
    kw = fake["captured"]
    assert kw["response_format"] == {"type": "json_object"}
    assert kw["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_tts_streaming_vllm_omni(fake):
    tts = clients.TextToSpeech({"model": "m", "base_url": "u", "voice": "vivian", "response_format": "pcm",
                                "sample_rate": 24000, "stream": True, "stream_format": "audio",
                                "language": "Chinese", "speed": 1.2})
    pcm = (np.ones(2400, np.int16) * 100).tobytes()
    fake["speech"] = ({"content-type": "audio/pcm", "x-audio-sample-rate": "24000"}, [pcm[:1001], pcm[1001:]])

    async def collect(t, text="你好"):
        return b"".join([c async for c in t.stream(text)])
    assert asyncio.run(collect(tts)) == pcm                      # 奇数字节切分也能正确拼接
    assert fake["captured"]["extra_body"] == {"stream": True, "stream_format": "audio", "language": "Chinese"}
    assert "speed" not in fake["captured"]                     # 流式时不能传 speed

    fake["speech"] = ({"content-type": "audio/pcm", "x-audio-sample-rate": "16000"}, [b"\x10\x00" * 1600])
    assert len(asyncio.run(collect(tts))) == 2400 * 2           # 按响应头采样率重采样

    fake["speech"] = ({"content-type": "text/event-stream"}, [b"data: x"])
    with pytest.raises(RuntimeError, match="stream_format"):
        asyncio.run(collect(tts))


def test_tts_wav(fake):
    tts = clients.TextToSpeech({"model": "m", "base_url": "u", "response_format": "wav", "sample_rate": 24000})
    fake["speech"] = ({"content-type": "audio/wav"}, [pcm_to_wav(b"\x01\x00" * 1200, 12000)])
    out = asyncio.run(_collect(tts))
    assert len(out) == 2400 * 2 and "extra_body" not in fake["captured"]


async def _collect(t):
    return b"".join([c async for c in t.stream("x")])
