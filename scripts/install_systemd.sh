#!/usr/bin/env bash
# 按 scripts/models.env 生成并安装 4 个 systemd 服务，实现开机自启、崩溃自动重启：
#   vva-vlm  看图模型（VLM_GPU:VLM_PORT）
#   vva-tts  语音合成（TTS_GPU:TTS_PORT）
#   vva-asr  语音识别（ASR_GPU:ASR_PORT，等 TTS 就绪后再启动，避免同卡抢显存）
#   vva-app  智能体网页服务（等 VLM 就绪后再启动）
#
#   sudo scripts/install_systemd.sh [--port 9000] [--user work] [--dry-run]
#
# --dry-run 只打印要写入的文件，不安装。修改 models.env 后重新运行本脚本即可更新服务。
# 装好后请统一用 systemctl 管理，不要再用 scripts/models.sh start，否则会抢端口和显存。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${MODELS_ENV:-$ROOT/scripts/models.env}"
# shellcheck disable=SC1090
source "$ENV_FILE"

APP_PORT=9000
RUN_USER="${SUDO_USER:-$(id -un)}"
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --port) APP_PORT="$2"; shift 2 ;;
    --user) RUN_USER="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) sed -n '2,11p' "$0"; exit 1 ;;
  esac
done

MODEL_HOST="${MODEL_HOST:-127.0.0.1}"
HF="${HF_HOME:-$HOME/.cache/huggingface}"
TTS_EAGER=""; [ "${TTS_ENFORCE_EAGER:-1}" = "1" ] && TTS_EAGER=" --enforce-eager"
WAIT='until curl -sf http://127.0.0.1:%s/v1/models >/dev/null; do sleep 5; done'

common() {   # common <venv> <gpu>
  cat <<EOF
User=$RUN_USER
WorkingDirectory=$ROOT
Environment=CUDA_VISIBLE_DEVICES=$2
Environment=VLLM_USE_FLASHINFER_SAMPLER=0
Environment=HF_HOME=$HF
Environment=PATH=$1/bin:/usr/local/bin:/usr/bin:/bin
Restart=always
RestartSec=10
TimeoutStartSec=1800
TimeoutStopSec=60
KillMode=control-group
EOF
}

unit_vlm() {
  cat <<EOF
[Unit]
Description=VVA vision model $VLM_NAME (GPU$VLM_GPU:$VLM_PORT)
After=network-online.target

[Service]
$(common "$VLLM_VENV" "$VLM_GPU")
ExecStart=$VLLM_VENV/bin/vllm serve $VLM_MODEL --served-model-name $VLM_NAME --host $MODEL_HOST --port $VLM_PORT --max-model-len $VLM_MAX_LEN --limit-mm-per-prompt '{"image": $VLM_MAX_IMAGES, "video": 0}' --gpu-memory-utilization $VLM_GPU_UTIL --enable-auto-tool-choice --tool-call-parser hermes ${VLM_EXTRA_ARGS:-}

[Install]
WantedBy=multi-user.target
EOF
}

unit_tts() {
  cat <<EOF
[Unit]
Description=VVA TTS $TTS_NAME (GPU$TTS_GPU:$TTS_PORT)
After=network-online.target

[Service]
$(common "$OMNI_VENV" "$TTS_GPU")
ExecStart=$OMNI_VENV/bin/vllm serve $TTS_MODEL --omni --served-model-name $TTS_NAME --host $MODEL_HOST --port $TTS_PORT --trust-remote-code$TTS_EAGER ${TTS_EXTRA_ARGS:-}

[Install]
WantedBy=multi-user.target
EOF
}

unit_asr() {
  cat <<EOF
[Unit]
Description=VVA ASR $ASR_NAME (GPU$ASR_GPU:$ASR_PORT)
After=vva-tts.service
Wants=vva-tts.service

[Service]
$(common "$VLLM_VENV" "$ASR_GPU")
ExecStartPre=/bin/bash -c '$(printf "$WAIT" "$TTS_PORT")'
ExecStart=$VLLM_VENV/bin/vllm serve $ASR_MODEL --served-model-name $ASR_NAME --host $MODEL_HOST --port $ASR_PORT --gpu-memory-utilization $ASR_GPU_UTIL --max-model-len 4096 ${ASR_EXTRA_ARGS:-}

[Install]
WantedBy=multi-user.target
EOF
}

unit_app() {
  cat <<EOF
[Unit]
Description=VVA agent web service (:$APP_PORT)
After=vva-vlm.service vva-tts.service vva-asr.service
Wants=vva-vlm.service vva-tts.service vva-asr.service

[Service]
User=$RUN_USER
WorkingDirectory=$ROOT
Environment=PATH=$ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStartPre=/bin/bash -c '$(printf "$WAIT" "$VLM_PORT")'
ExecStart=$ROOT/scripts/run_app.sh --port $APP_PORT
Restart=always
RestartSec=5
TimeoutStartSec=1800

[Install]
WantedBy=multi-user.target
EOF
}

for name in vlm tts asr app; do
  if [ "$DRY" = 1 ]; then
    echo "# ===== /etc/systemd/system/vva-$name.service"; "unit_$name"; echo
  else
    "unit_$name" > "/etc/systemd/system/vva-$name.service"
    echo "已写入 /etc/systemd/system/vva-$name.service"
  fi
done

if [ "$DRY" = 0 ]; then
  systemctl daemon-reload
  echo "完成。首次启用：sudo systemctl enable --now vva-vlm vva-tts vva-asr vva-app"
  echo "已在运行时使改动生效：sudo systemctl restart vva-vlm vva-tts vva-asr vva-app"
fi
