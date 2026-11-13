# 实时语音视频分析智能体（多模态大模型 + 智能体）

操作员在监控室用语音向摄像头下达指令，智能体查看监控画面后简短回答：可以问画面里的情况、读表、回看刚才发生了什么，
查询“今天上午 9 点左右发生了什么”这类历史记录，也可以设置“当……时提醒我”。
所有模型都通过 **OpenAI 兼容接口**调用，模型由你自行部署后接入。

## 目录结构

```
main.py                     启动入口
config.example.yaml         配置模板（复制为 config.yaml）
vva/
  core/        事件总线、数据类型、配置加载、插件加载
  video/       视频接入层 ── base.py 抽象接口
               opencv_source.py  本地摄像头 / RTSP / 视频文件
               push_source.py    外部推帧
               frame_buffer.py   帧缓存（回看用）
  audio/       音频接入层 ── base.py 抽象接口
               local.py          电脑麦克风 / 扬声器
               push.py           外部推流（WebSocket）
               vad.py            语音活动检测与断句
  models/      OpenAI 格式客户端：对话 LLM、视觉 VLM、ASR、TTS
  perception/  后台观察、画面时间线、监控规则、图像工具
  agent/       智能体（函数调用循环）、工具基类与视觉工具、提示词
  memory/      长期记忆 ── store.py      SQLite 存储（记录、摘要、备忘、规则）
               keyframes.py  关键帧图片
               recorder.py   订阅事件写入记忆、按期限清理
               summarizer.py 每 10 分钟生成时段摘要
               tools.py      search_memory / look_at_time / remember_note 等工具
  voice/       语音会话（断句→识别→过滤→播报、打断、唤醒词、按住说话）
  app.py       编排：把以上模块组装起来
  web/         网页服务与测试页面
examples/      自定义摄像头引擎、跨进程推帧/推音频示例
tests/         用假模型跑通全流程的测试
```

模块之间只依赖接口：视频源只负责产出帧，音频源只负责产出/播放 PCM，智能体只依赖 `llm.stream()` 和工具，
语音会话只依赖 `asr.transcribe()` / `tts.stream()`。替换任何一层都不影响其他层。

> **在 Linux 服务器上部署（含 2×3090 模型部署脚本）请直接看 [DEPLOY_LINUX.md](DEPLOY_LINUX.md)。**

## 最简单的本地测试：浏览器测试页

不需要安装 sounddevice，也不用排查设备编号，摄像头和麦克风都由浏览器调用：

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml     # 只需填写模型地址
python main.py -c config.yaml --browser
```

用 Chrome 或 Edge 打开 `http://localhost:8000/browser`：

1. 点“开启摄像头”，允许浏览器使用摄像头。画面会按选择的频率（默认每秒 2 帧）上传给服务端；
2. 点“开启麦克风”，直接说话即可，回答会在浏览器里播放；
3. 也可以在输入框打字提问。还没部署语音识别/合成时，用 `--browser --no-audio` 启动，只测文字和看图。

说明：浏览器只有在 `localhost` 或 HTTPS 下才允许使用摄像头和麦克风。用其他电脑访问时，需要给服务配置 HTTPS。

## 快速开始（服务端直接打开摄像头和麦克风）

```bash
pip install -r requirements.txt sounddevice   # 本机麦克风/扬声器需要 sounddevice；Linux 还需 sudo apt install libportaudio2
cp config.example.yaml config.yaml     # 填写模型地址
python main.py --probe-cameras         # 查看摄像头编号
python main.py --list-devices          # 查看麦克风/扬声器编号
python main.py -c config.yaml          # 启动
```

浏览器打开 `http://localhost:8000`，可以看到实时画面、对话框和画面时间线。直接对着电脑麦克风说话，或在页面输入文字。

- 没有摄像头？把 `video.type` 设为 `custom`，使用 `examples/custom_video_source.py:TestPatternSource`，会生成一块会动的指针表盘画面，可直接测试读表。
- 先不接语音？`python main.py --no-audio`，只用页面文字交互（此时不需要 ASR/TTS 服务）。

## 模型接口要求

| 配置项 | 接口 | 要求 |
|---|---|---|
| `models.vlm` | `POST /v1/chat/completions` | 支持 `image_url`（base64 data URL）多图输入 |
| `models.agent_llm` | `POST /v1/chat/completions` | 支持 `tools` 函数调用和流式输出；不填则复用 VLM |
| `models.asr` | `POST /v1/audio/transcriptions` | 接收 wav 文件，返回 `{"text": ...}` |
| `models.tts` | `POST /v1/audio/speech` | `response_format=pcm`（16-bit 单声道，流式）或 `wav` |

几个需要注意的配置：

- **`vlm.bbox_format`**：不同模型输出定位框的坐标格式不同，必须与模型对应，否则“放大看”会框错位置。
  `0-1000`（如 Qwen3-VL、Gemini）、`0-1`、`pixel`（如 Qwen2.5-VL，相对于送入图片的像素）。
- **`tts.sample_rate`**：必须与 TTS 服务返回的 PCM 采样率一致（OpenAI 规范为 24000），否则声音会变调。
- **推理模型**：如果模型会输出 `<think>` 思考内容，系统会自动过滤，但会增加延迟。建议用 `extra_body` 关闭思考（模板中有示例）。
- 如果用 vLLM 部署多模态模型做智能体大脑，需开启函数调用，例如 `--enable-auto-tool-choice --tool-call-parser <对应解析器>`。

## 接入其他摄像头

### 方式一：流地址（RTSP / HTTP / 文件）
```yaml
video:
  type: stream
  stream: {url: "rtsp://user:pass@ip:554/...", rtsp_transport: tcp}
```

### 方式二：自定义引擎插件（同进程，推荐用于厂商 SDK、GB28181 平台等）
继承 `vva.video.base.VideoSource`，实现 `start()` / `stop()`，拿到画面后调用 `self._emit(bgr_image)`：
```yaml
video:
  type: custom
  custom:
    class: "my_engines/hik.py:HikSource"     # 或 "my_package.module:HikSource"
    params: {ip: 192.168.1.64, user: admin, password: xxx}
```
`examples/custom_video_source.py` 中有可运行的测试源和 SDK 接入模板。

### 方式三：跨进程推帧（其他语言的程序、独立采集服务）
```yaml
video:
  type: push
```
外部程序把 JPEG 字节 `POST` 到 `/api/ingest/frame`（可选请求头 `X-Timestamp`、`X-Token`）。示例见 `examples/push_frames_client.py`。

## 接入其他音频设备

- **自定义插件**：继承 `vva.audio.base.AudioInput`（调用 `self._emit(pcm)` 送入麦克风数据）和 `AudioOutput`（实现 `write` / `clear` / `is_playing`），配置 `type: custom` 和 `custom.class`。
- **WebSocket 推流**：配置 `audio.input.type: push` 和 `audio.output.type: push`，外部程序连接 `/api/ingest/audio`：
  1. 连接后服务端先发送 `{"type":"config","input_sample_rate":16000,"output_sample_rate":24000}`；
  2. 客户端发送二进制 PCM（16-bit 小端、单声道），作为麦克风输入；
  3. 服务端发送二进制 PCM，作为要播放的语音；
  4. 服务端发送 `{"type":"clear"}` 时，客户端应立即停止播放（用户打断）。

  示例见 `examples/push_audio_client.py`。

## 智能体能力

| 工具 | 作用 |
|---|---|
| `look_now(question, region?)` | 看当前画面；带 `region` 时裁剪并放大局部 |
| `locate(target)` | 定位目标，返回 0~1 的框坐标（同时在网页画面上画框） |
| `look_back(seconds_ago, question)` | 看过去某一时刻的画面 |
| `look_sequence(seconds, question, frames)` | 按时间顺序看多帧，判断动作和变化 |
| `search_timeline(minutes)` | 查最近两小时内后台记录的文字时间线 |
| `search_memory(start, end, keyword?)` | 按时间段查长期记录（时段摘要、画面记录、提醒、指令），可查到几天前 |
| `look_at_time(time, question)` | 调出长期记忆中某一时刻保存的画面再看一次 |
| `add_watch` / `list_watches` / `remove_watch` | 管理监控提醒（规则会持久化，重启后恢复） |
| `remember_note` / `forget_note` | 记下 / 删除操作员交代的固定信息，回答时自动参考 |

新增工具：继承 `vva/agent/toolkit.py` 的 `ToolProvider`，在 `schemas()` 里加定义并实现 `tool_<名称>` 方法，再在 `app.py` 里加入 `ToolSet`。

### 角色与回答风格

智能体的定位是“监控值守助手”：下指令的操作员不在画面里，画面中的人都是被监控对象；只处理与监控有关的问题，
不寒暄、不反问。回答默认一句话，提示词要求不超过 `agent.max_answer_chars` 的一半，超过 `max_answer_chars`
时在句末截断（兜底）。

### 收音模式（`voice.mode`）

| 模式 | 说明 |
|---|---|
| `always` | 一直在听，自动断句（默认） |
| `wake_word` | 句子里要带唤醒词（`voice.wake_words`）才响应；唤醒后 `wake_timeout` 秒内不用再说 |
| `push_to_talk` | 按住网页上的“按住说话”按钮或空格键说话，松开后识别，最不容易误触发 |

防误触发分三层：人声检测（webrtcvad + 频谱 + 音量）→ 连续 240ms 人声才算开始、人声不足 0.5 秒或占比不足一半整句丢弃 →
识别结果太短、只有语气词或常见误识别文字（如“谢谢观看”）时丢弃。参数见配置的 `vad` 和 `voice` 段。

### 长期记忆（`memory`）

后台观察的每条画面记录、提醒、操作员指令都会写入 `data/memory/memory.db`，同时保存一张关键帧到 `data/memory/frames/`；
每 10 分钟生成一段时段摘要。文字默认保留 30 天，图片保留 7 天，到期自动清理。监控规则和备忘也存在这里，重启后恢复。
记录的粒度取决于后台观察的间隔（默认几秒一次），一两秒内的短暂动作可能没被记录到。
提示词集中在 `vva/agent/prompts.py`，可按场景调整；也可在配置中填写 `agent.scene_hint` 描述场景。

## 常见问题

- **系统自己听到自己说话**：电脑外放时扬声器的声音会进入麦克风。默认 `voice.mute_mic_while_speaking: true`（播放时不收音），代价是不能“说话即打断”。戴耳机或使用带回声消除的会议麦克风时，改为 `false` 即可边说边打断。网页上的“打断”按钮任何时候都可用。
- **环境吵、误触发**：确认已安装 `webrtcvad-wheels`（日志里有“VAD 使用 webrtcvad”）；调高 `vad.threshold_db`（如 -35）或 `vad.start_frames`；或改用 `voice.mode: wake_word` / `push_to_talk`。
- **正常说话却没反应**：调低 `vad.min_band_ratio`（如 0.5）或 `vad.threshold_db`（如 -45）；日志里的“忽略识别结果”会写明丢弃原因。
- **回答还是太长 / 太短**：调整 `agent.max_answer_chars`；需要过渡语可设 `agent.filler_text: "稍等。"`。
- **读数不准**：在 `agent.scene_hint` 中说明表的量程和单位；适当提高摄像头分辨率；必要时调大 `agent.crop_min_side`。
- **调用量太大**：调大 `observer.interval` 和 `observer.change_threshold`，或设 `observer.enabled: false` 关闭后台观察（关闭后不能回答时间线类问题，也不能主动提醒）。
- **监控规则的局限**：规则基于单帧画面 + 上一次的描述来判断，“持续超过 5 分钟”这类时长条件需要扩展 `perception/observer.py` 中的状态逻辑。

## 测试

```bash
pip install pytest
python -m pytest -q tests
```
测试使用假模型，不需要摄像头、麦克风和模型服务，覆盖帧缓存、VAD（含敲键盘噪声）、句子切分与回答限长、智能体工具循环、
后台观察与告警、语音会话（唤醒词、打断、按住说话、识别结果过滤）、长期记忆（记录、按时间查询、回看、摘要、清理、规则恢复）和整体编排。
