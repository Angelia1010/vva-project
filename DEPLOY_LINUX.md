# Linux 部署指南（2 × RTX 3090）

部署完成后的结构：

```
GPU0 (3090) ── Qwen3-VL-8B-Instruct   看图 + 智能体大脑   127.0.0.1:8001
GPU1 (3090) ── Qwen3-ASR-1.7B         语音识别             127.0.0.1:8002
            └─ Qwen3-TTS-1.7B         语音合成             127.0.0.1:8003
智能体服务（本项目） ─────────────────────────────────── 0.0.0.0:8000  ← 浏览器访问
```

模型服务只监听本机，对外只开放智能体服务的 8000 端口。

---

## 0. 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Ubuntu 22.04 / 24.04（其他发行版类似） |
| 显卡驱动 | 较新的 NVIDIA 驱动（建议 550 以上），`nvidia-smi` 能看到两张卡 |
| Python | 3.10 ~ 3.12 |
| 磁盘 | 模型约 30 GB，加上 Python 环境，建议预留 80 GB |
| 内存 | 32 GB 以上 |

```bash
nvidia-smi                                   # 确认两张 3090 都在
sudo apt update && sudo apt install -y python3-venv curl openssl
curl -LsSf https://astral.sh/uv/install.sh | sh   # 安装 uv（比 pip 快得多），装完重新登录终端
```

## 1. 上传代码

```bash
mkdir -p ~/apps && cd ~/apps
unzip vision-voice-agent.zip          # 解压后目录为 vva-project
cd vva-project
chmod +x scripts/*.sh
```

## 2. 安装三个 Python 环境

vLLM 和 vLLM-Omni 的依赖版本不同，分开安装最省事。

```bash
# ① 模型环境：跑看图模型和语音识别
uv venv ~/venvs/vllm -p 3.12
source ~/venvs/vllm/bin/activate
uv pip install -U "vllm[audio]" --torch-backend=auto
deactivate

# ② 语音合成环境：vLLM-Omni
uv venv ~/venvs/vllm-omni -p 3.12
source ~/venvs/vllm-omni/bin/activate
uv pip install -U vllm --torch-backend=auto
uv pip install git+https://github.com/vllm-project/vllm-omni.git
deactivate

# ③ 应用环境：本项目
cd ~/apps/vva-project
uv venv .venv -p 3.12
source .venv/bin/activate
uv pip install -r requirements.txt pytest
python -m pytest -q tests        # 应显示全部通过
deactivate
```

> 如果 vLLM-Omni 安装时与 vLLM 版本冲突，按其官方文档指定的 vLLM 版本重新安装。
> 国内网络安装慢时，可加镜像：`uv pip install ... --index-url https://mirrors.aliyun.com/pypi/simple/`。

## 3. 下载模型（推荐提前下载）

首次启动时 vLLM 会自动下载，但在国内网络下很慢，建议先下载到本地目录：

```bash
source ~/venvs/vllm/bin/activate
uv pip install modelscope
mkdir -p /data/models
modelscope download --model Qwen/Qwen3-VL-8B-Instruct              --local_dir /data/models/Qwen3-VL-8B-Instruct
modelscope download --model Qwen/Qwen3-ASR-1.7B                    --local_dir /data/models/Qwen3-ASR-1.7B
modelscope download --model Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice   --local_dir /data/models/Qwen3-TTS-12Hz-1.7B-CustomVoice
deactivate
```

> 也可以用 HuggingFace 镜像：`export HF_ENDPOINT=https://hf-mirror.com`，再用 `huggingface-cli download <模型名> --local-dir <目录>`。

然后修改 `scripts/models.env`，把模型改成本地路径：

```bash
VLM_MODEL=/data/models/Qwen3-VL-8B-Instruct
ASR_MODEL=/data/models/Qwen3-ASR-1.7B
TTS_MODEL=/data/models/Qwen3-TTS-12Hz-1.7B-CustomVoice
```

同时确认 `VLLM_VENV`、`OMNI_VENV` 与第 2 步的路径一致。

## 4. 启动模型服务

```bash
cd ~/apps/vva-project
scripts/models.sh start          # 启动全部：VLM 与 TTS 并行启动，TTS 就绪后再启动 ASR
scripts/models.sh status         # 查看状态和显存
scripts/models.sh logs vlm       # 跟踪某个服务的日志（Ctrl+C 退出）
scripts/models.sh stop           # 全部停止；也可 stop vlm / restart tts
```

首次启动会加载模型、编译 CUDA 图，看图模型通常需要几分钟。启动顺序这样安排的原因是：语音识别和语音合成共用 GPU1，脚本会在语音合成启动后读取剩余显存，再自动设定语音识别的显存占比，避免第二个服务因显存不足启动失败。

## 5. 自检模型

```bash
source .venv/bin/activate
cp config.2x3090.yaml config.yaml
python scripts/check_models.py -c config.yaml
```

正常输出类似：

```
[通过] 看图问答  回答：2580  耗时 0.8s
[通过] 函数调用  调用：look_now({"question": "..."})
[通过] 定位坐标（bbox_format=0-1000）  ... IoU=0.85
[通过] 流式合成  首包 0.35s ... 分 12 包
[通过] 语音识别  识别：你好，这是语音合成测试……
全部通过 ✅
```

- 合成的测试语音保存在 `check_output/tts.wav`，可以下载下来听听效果；
- `python scripts/check_models.py -c config.yaml --voices` 列出可用音色，把喜欢的填到 `config.yaml` 的 `models.tts.voice`。

## 6. 启动智能体服务

```bash
source .venv/bin/activate
scripts/run_app.sh                # 浏览器模式；还没部署语音时用 scripts/run_app.sh --no-audio
```

### 用浏览器访问

浏览器**只在 `localhost` 或 HTTPS 页面上允许使用摄像头和麦克风**。服务器通常没有显示器，你会从自己的电脑访问它，所以下面两种方式选一种：

**方式 A：SSH 端口转发（最简单，推荐测试时用）**

在你自己的电脑上执行（Windows 可用 PowerShell）：

```bash
ssh -L 8000:127.0.0.1:8000 用户名@服务器IP
```

保持这个窗口不关，然后在自己电脑的 Chrome/Edge 打开 **http://localhost:8000/browser**。对浏览器来说这就是 localhost，摄像头和麦克风都能用。

**方式 B：HTTPS（多人或长期使用）**

```bash
scripts/gen_cert.sh 服务器IP          # 生成自签名证书到 certs/
sudo ufw allow 8000/tcp               # 如果开了防火墙
scripts/run_app.sh                    # 检测到 certs/ 会自动启用 HTTPS
```

浏览器打开 **https://服务器IP:8000/browser**，提示“不安全”时选择“继续访问”。

### 页面使用

1. 点“开启摄像头”，再点“开启麦克风”，允许浏览器权限；
2. 直接说话，例如：“画面里有几个人？”“桌上有什么？”“有人离开座位就提醒我”；
3. 右侧会显示识别出的文字、智能体调用的工具和回答，画面上会画出定位框；
4. 左下方是后台观察生成的时间线。

## 7. 接入真实摄像头（RTSP）

修改 `config.yaml`：

```yaml
video:
  type: stream
  stream:
    url: "rtsp://admin:密码@192.168.1.64:554/Streaming/Channels/101"
```

重启 `scripts/run_app.sh`。浏览器测试页会自动改为显示摄像头画面（不再显示“开启摄像头”按钮），麦克风仍用浏览器的，可以直接对话。

## 8. 开机自启（可选）

`/etc/systemd/system/vva-models.service`：

```ini
[Unit]
Description=VVA model services
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=你的用户名
WorkingDirectory=/home/你的用户名/apps/vva-project
ExecStart=/home/你的用户名/apps/vva-project/scripts/models.sh start
ExecStop=/home/你的用户名/apps/vva-project/scripts/models.sh stop
TimeoutStartSec=1800

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/vva-app.service`：

```ini
[Unit]
Description=VVA agent web service
After=vva-models.service
Requires=vva-models.service

[Service]
User=你的用户名
WorkingDirectory=/home/你的用户名/apps/vva-project
ExecStart=/home/你的用户名/apps/vva-project/scripts/run_app.sh
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vva-models vva-app
journalctl -u vva-app -f
```

## 9. 常见问题

| 现象 | 原因与处理 |
|---|---|
| 看图模型启动报显存不足 | 在 `models.env` 把 `VLM_MAX_LEN` 降到 12288，或把 `VLM_GPU_UTIL` 调到 0.90 |
| ASR 提示剩余显存不足 | TTS 占用太多：在 `models.env` 加 `TTS_EXTRA_ARGS="--gpu-memory-utilization 0.4"` 后重启 TTS 和 ASR；或把 ASR 换成 Qwen3-ASR-0.6B |
| TTS 启动失败，日志提示缺少部署配置 | 在 `models.env` 加 `TTS_EXTRA_ARGS="--deploy-config <vllm-omni 安装目录>/vllm_omni/deploy/qwen3_tts.yaml"` |
| TTS 启动报 `--served-model-name` 不支持 | 删掉 `models.sh` 中 TTS 的这个参数，并把 `config.yaml` 的 `models.tts.model` 改成 `TTS_MODEL` 的完整值 |
| 自检“函数调用”失败 | 确认 VLM 启动参数有 `--enable-auto-tool-choice --tool-call-parser hermes`（换成 Qwen3.5/3.6 系列要改为 `qwen3_coder`） |
| 自检“定位坐标”失败 | 按提示修改 `models.vlm.bbox_format` |
| 播放出来是噪音 | TTS 返回的不是原始 PCM：确认 `stream_format: audio`；或把 `response_format` 改为 `wav`、`stream` 改为 `false`（会慢一些） |
| 声音变调（太快或太慢） | `models.tts.sample_rate` 与服务实际采样率不一致；Qwen3-TTS 为 24000 |
| 页面提示无法使用摄像头/麦克风 | 没有通过 localhost 或 HTTPS 访问，见第 6 步 |
| 系统听到自己说话、反复打断 | 保持 `voice.mute_mic_while_speaking: true`；或戴耳机 |
| 环境吵、经常误识别 | 调高 `vad.threshold_db`（如 -38），或设置唤醒词 `voice.wake_words: ["小智"]` |
| 回答慢 | 把 `agent.look_max_side` 降到 896、`observer.interval` 调大；或关闭后台观察 `observer.enabled: false` |
| 读表不准 | 在 `agent.scene_hint` 写清表的量程、单位；让摄像头离表近一些；8B 模型能力有限时考虑升级更大的模型 |

### 日志位置

- 模型服务：`logs/vlm.log`、`logs/asr.log`、`logs/tts.log`
- 智能体服务：终端输出，或 `journalctl -u vva-app`
