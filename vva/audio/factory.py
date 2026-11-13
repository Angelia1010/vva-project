"""根据配置创建音频输入/输出。新增引擎只需继承 AudioInput/AudioOutput 并配置 custom.class。"""
from __future__ import annotations

from typing import Any

from vva.audio.base import AudioInput, AudioOutput, NullAudioOutput
from vva.core.plugin import load_class


def create_audio_input(cfg: dict[str, Any]) -> AudioInput:
    t = (cfg.get("type") or "local").lower()
    sr = int(cfg.get("sample_rate", 16000))
    if t == "local":
        from vva.audio.local import LocalMicrophone
        return LocalMicrophone(sample_rate=sr, device=cfg.get("device"))
    if t == "push":
        from vva.audio.push import PushAudioInput
        return PushAudioInput(sample_rate=sr)
    if t == "custom":
        custom = cfg.get("custom", {})
        obj = load_class(custom["class"])(sample_rate=sr, **(custom.get("params") or {}))
        if not isinstance(obj, AudioInput):
            raise TypeError(f"{custom['class']} 必须继承 vva.audio.base.AudioInput")
        return obj
    raise ValueError(f"未知的 audio.input.type: {t}（可选 local / push / custom）")


def create_audio_output(cfg: dict[str, Any], sample_rate: int) -> AudioOutput:
    t = (cfg.get("type") or "local").lower()
    if t == "local":
        from vva.audio.local import LocalSpeaker
        return LocalSpeaker(sample_rate=sample_rate, device=cfg.get("device"))
    if t == "push":
        from vva.audio.push import CallbackAudioOutput
        return CallbackAudioOutput(sample_rate=sample_rate)
    if t == "none":
        return NullAudioOutput(sample_rate=sample_rate)
    if t == "custom":
        custom = cfg.get("custom", {})
        obj = load_class(custom["class"])(sample_rate=sample_rate, **(custom.get("params") or {}))
        if not isinstance(obj, AudioOutput):
            raise TypeError(f"{custom['class']} 必须继承 vva.audio.base.AudioOutput")
        return obj
    raise ValueError(f"未知的 audio.output.type: {t}（可选 local / push / none / custom）")
