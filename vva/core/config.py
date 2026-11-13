"""配置加载：YAML 文件 + 默认值合并 + ${ENV_VAR} 环境变量展开。"""
from __future__ import annotations

import copy
import os
import re
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "video": {
        "type": "local",
        "source_id": "cam0",
        "local": {"index": 0, "width": 1280, "height": 720, "fps": 15, "backend": None},
        "stream": {"url": "", "loop": False, "rtsp_transport": "tcp"},
        "push": {},
        "custom": {"class": "", "params": {}},
        "buffer": {"seconds": 60, "fps": 2.0, "jpeg_quality": 85, "max_side": 1920},
    },
    "audio": {
        "enabled": True,
        "input": {"type": "local", "sample_rate": 16000, "device": None,
                  "custom": {"class": "", "params": {}}},
        "output": {"type": "local", "device": None,
                   "custom": {"class": "", "params": {}}},
    },
    "vad": {
        "engine": "auto",            # auto | energy | webrtc
        "frame_ms": 30,
        "threshold_db": -45.0,
        "margin_db": 12.0,
        "start_frames": 3,
        "end_silence_ms": 700,
        "pre_roll_ms": 300,
        "min_utterance_ms": 300,
        "max_utterance_s": 15.0,
        "webrtc_mode": 2,
    },
    "models": {
        "agent_llm": {"base_url": "", "api_key": "", "model": "", "temperature": 0.3,
                      "stream": True, "timeout": 60, "extra_body": None},
        "vlm": {"base_url": "", "api_key": "", "model": "", "temperature": 0.1,
                "timeout": 60, "max_image_side": 1280, "bbox_format": "0-1000",
                "json_mode": False, "max_tokens": 1024, "extra_body": None},
        "asr": {"base_url": "", "api_key": "", "model": "", "language": "zh",
                "prompt": "", "timeout": 30, "extra_body": None},
        "tts": {"base_url": "", "api_key": "", "model": "", "voice": "alloy",
                "response_format": "pcm", "sample_rate": 24000, "speed": None,
                "stream": True, "stream_format": "audio", "language": "",
                "timeout": 60, "extra_body": None},
    },
    "observer": {
        "enabled": True,
        "interval": 4.0,
        "max_idle": 30.0,
        "rule_check_interval": 8.0,
        "change_threshold": 6.0,
        "image_side": 1024,
        "timeline_minutes": 120,
        "scene_hint": "",
    },
    "agent": {
        "max_history_turns": 8,
        "max_tool_rounds": 6,
        "filler_text": "我看一下。",
        "scene_hint": "",
        "look_max_side": 1280,
        "crop_min_side": 768,
    },
    "voice": {
        "barge_in": True,
        "mute_mic_while_speaking": True,
        "mute_tail_ms": 400,
        "wake_words": [],
        "wake_timeout": 30.0,
    },
    "web": {"host": "0.0.0.0", "port": 8000, "mjpeg_fps": 10, "ingest_token": "",
            "ssl_certfile": "", "ssl_keyfile": ""},
}

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: str | None) -> dict[str, Any]:
    user: dict[str, Any] = {}
    if path:
        with open(path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    cfg = deep_merge(DEFAULTS, user)
    cfg = _expand_env(cfg)
    # 未单独配置的模型，沿用 vlm 的地址和 Key，减少重复填写
    vlm = cfg["models"]["vlm"]
    for name in ("agent_llm", "asr", "tts"):
        m = cfg["models"][name]
        if not m.get("base_url"):
            m["base_url"] = vlm.get("base_url", "")
        if not m.get("api_key"):
            m["api_key"] = vlm.get("api_key", "")
    if not cfg["models"]["agent_llm"].get("model"):
        cfg["models"]["agent_llm"]["model"] = vlm.get("model", "")
    return cfg
