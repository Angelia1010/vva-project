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
        "threshold_db": -40.0,       # 低于该音量不算人声
        "margin_db": 12.0,           # 需高出环境噪声底多少 dB
        "start_frames": 6,           # 最近 start_window_ms 内至少多少帧人声才算开始说话，挡住敲键盘等短促声音
        "start_window_ms": 300,
        "end_silence_ms": 700,
        "pre_roll_ms": 300,
        "min_utterance_ms": 500,     # 一句话里人声至少多长
        "min_voiced_ratio": 0.5,     # 一句话里人声帧至少占多少
        "min_band_ratio": 0.6,       # 人声频带（80~4000Hz）能量占比下限，0 表示不检查
        "max_utterance_s": 15.0,
        "webrtc_mode": 2,            # webrtcvad 严格程度 0~3，越大越容易把声音判为非人声
        "debug": False,              # true：每 2 秒在日志里打印音量和各项检查的通过情况，用于调参
    },
    "models": {
        "agent_llm": {"base_url": "", "api_key": "", "model": "", "temperature": 0.3,
                      "stream": True, "timeout": 60, "max_tokens": 400, "extra_body": None},
        "vlm": {"base_url": "", "api_key": "", "model": "", "temperature": 0.1,
                "timeout": 60, "max_image_side": 1280, "bbox_format": "0-1000",
                "json_mode": False, "max_tokens": 1024, "extra_body": None},
        "asr": {"base_url": "", "api_key": "", "model": "", "language": "zh",
                "prompt": "", "timeout": 30, "extra_body": None},
        "embedding": {"base_url": "", "api_key": "", "model": "", "timeout": 30, "batch_size": 32,
                      "extra_body": None},      # 可选：知识库向量检索；不填 model 则只用关键词检索
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
        "filler_text": "",           # 调工具前先播报的话（如“稍等。”）；空字符串表示不播报
        "scene_hint": "",
        "look_max_side": 1280,
        "crop_min_side": 768,
        "max_answer_chars": 60,      # 回答长度硬上限（字），超出后在句末截断；提示词目标为它的一半
        "look_max_tokens": 256,      # 看图模型单次回答的 token 上限
        "force_tool_first": True,    # 每个问题第一轮强制调用工具（先看画面/查记录），避免小模型不看就答或拒答
    },
    "voice": {
        "mode": "always",            # always | wake_word | push_to_talk
        "barge_in": True,
        "mute_mic_while_speaking": True,
        "mute_tail_ms": 400,
        "wake_words": [],            # mode=wake_word 时使用，如 ["小监"]
        "wake_timeout": 30.0,        # 唤醒后多少秒内的后续指令不必再说唤醒词
        "min_chars": 2,              # 识别结果少于几个字就丢弃
        "ignore_phrases": None,      # 要丢弃的识别结果（语气词、常见误识别）；None 用内置列表
        "ptt_min_ms": 300,           # 按住说话时，短于该时长的按下视为误触
    },
    "skills": {
        "enabled": True,
        "dir": "skills",             # 技能目录（相对于启动目录），可在网页 /skills 页面管理
        "legacy_scene_dir": "scenes/default",   # 旧版场景包：技能目录为空时自动转换成“现场概况”技能
        "max_ref_images": 3,         # 每个目标最多带几张参考图
        "max_objects_per_call": 2,   # 一次判断最多涉及几个目标（8B 模型一次最多 8 张图）
        "always_max_chars": 800,     # 常驻技能正文写进提示词的最大字数
    },
    "knowledge": {
        "enabled": True,
        "dir": "data/knowledge",     # 文档与索引目录；在网页 /knowledge 页面上传
        "chunk_chars": 400,          # 切分后每段的最大字数
        "top_k": 4,                  # 每次检索取几段
        "max_answer_chars": 150,     # 子智能体结论的最大字数（主智能体会再压缩成一句话）
        "max_file_mb": 50,
    },
    "memory": {
        "enabled": True,
        "dir": "data/memory",        # 数据库和关键帧图片的目录（相对于启动目录）
        "retention_days": 30,        # 文字记录保留天数
        "save_keyframes": True,      # 是否保存关键帧图片，供回看当时的画面
        "keyframe_retention_days": 7,
        "keyframe_side": 640,
        "keyframe_quality": 80,
        "summary_minutes": 10,       # 每多少分钟生成一段摘要；0 表示不生成
        "summary_catchup_hours": 24, # 重启后补做最近多少小时内缺失的摘要
        "look_max_gap_s": 300,       # look_at_time 找不到正好那一刻的画面时，前后最多放宽多少秒
        "max_events": 40,            # search_memory 一次最多返回多少条记录
        "max_notes": 50,
    },
    "channels": {                    # 接入入口，见 vva/channels
        "feishu": {
            "enabled": False,
            "app_id": "",
            "app_secret": "",
            "domain": "https://open.feishu.cn",
            "allowed_chats": [],     # 允许使用的群 chat_id；与 allowed_users 都为空时所有人可用
            "allowed_users": [],     # 允许使用的人 open_id
            "admin_users": [],       # 可以修改提醒规则、备忘的人；为空时所有可用的人都能改
            "require_mention": True, # 群聊里需要 @机器人
            "alert_chats": [],       # 告警推送到哪些群
            "report_chats": [],      # 定时报告发到哪些群
            "report_times": [],      # 报告时间，如 ["08:00", "18:00"]
            "web_url": "",           # 卡片上“查看现场”按钮的链接，如 https://124.128.90.162:9000/browser
            "send_images": True,     # 回答和告警是否附带画面截图
        },
    },
    "web": {"host": "0.0.0.0", "port": 8000, "mjpeg_fps": 10, "ingest_token": "",
            "admin_token": "",       # 技能、知识库管理页的修改口令；为空则不校验
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
    for name in ("agent_llm", "asr", "tts", "embedding"):
        m = cfg["models"][name]
        if not m.get("base_url"):
            m["base_url"] = vlm.get("base_url", "")
        if not m.get("api_key"):
            m["api_key"] = vlm.get("api_key", "")
    if not cfg["models"]["agent_llm"].get("model"):
        cfg["models"]["agent_llm"]["model"] = vlm.get("model", "")
    return cfg
