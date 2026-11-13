#!/usr/bin/env bash
# 模型服务管理：VLM（看图+大脑）、ASR（语音识别）、TTS（语音合成）
#
#   scripts/models.sh start [all|vlm|asr|tts]     启动（默认 all，顺序：VLM 与 TTS 并行，TTS 就绪后再启 ASR）
#   scripts/models.sh stop  [all|vlm|asr|tts]     停止
#   scripts/models.sh restart [all|vlm|asr|tts]
#   scripts/models.sh status                      查看运行状态与显存
#   scripts/models.sh logs  vlm|asr|tts           跟踪日志
#
# 配置见 scripts/models.env（可用环境变量 MODELS_ENV 指定其他文件）。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${MODELS_ENV:-$ROOT/scripts/models.env}"
# shellcheck disable=SC1090
source "$ENV_FILE"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib_models.sh"      # 启动参数（与 install_systemd.sh 共用）
READY_TIMEOUT="${READY_TIMEOUT:-1800}"    # 首次启动要下载模型，等待时间放宽
LOG_DIR="$ROOT/logs"; RUN_DIR="$ROOT/run"
mkdir -p "$LOG_DIR" "$RUN_DIR"

c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_red()   { printf '\033[31m%s\033[0m\n' "$*"; }
c_yel()   { printf '\033[33m%s\033[0m\n' "$*"; }

port_of() { case "$1" in vlm) echo "$VLM_PORT";; asr) echo "$ASR_PORT";; tts) echo "$TTS_PORT";; esac; }
pid_of()  { [ -f "$RUN_DIR/$1.pid" ] && cat "$RUN_DIR/$1.pid"; }
alive()   { local p; p="$(pid_of "$1")"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }
ready()   { curl -sf -m 3 "http://127.0.0.1:$(port_of "$1")/v1/models" >/dev/null 2>&1; }

need_venv() {
  if [ ! -x "$1/bin/vllm" ]; then
    c_red "虚拟环境 $1 中没有 vllm，请先按 DEPLOY_LINUX.md 安装，或修改 scripts/models.env"
    exit 1
  fi
}

# 在独立会话中后台启动，便于整组结束子进程
launch() {   # launch <name> <venv> <gpu> <命令...>
  local name="$1" venv="$2" gpu="$3"; shift 3
  if alive "$name"; then c_yel "$name 已在运行 (pid $(pid_of "$name"))"; return 0; fi
  need_venv "$venv"
  local log="$LOG_DIR/$name.log"
  echo "启动 $name（GPU $gpu，端口 $(port_of "$name")），日志：$log"
  echo "  命令：vllm $(render_args "${@:2}")"
  (
    # shellcheck disable=SC1091
    source "$venv/bin/activate"
    export PATH="$venv/bin:$PATH"
    export CUDA_VISIBLE_DEVICES="$gpu"      # 可以是多张卡，如 0,1（张量并行）
    apply_extra_env
    exec setsid "$@" >>"$log" 2>&1 < /dev/null
  ) &
  echo $! > "$RUN_DIR/$name.pid"
}

wait_ready() {   # wait_ready <name>
  local name="$1" t=0
  printf '等待 %s 就绪' "$name"
  while ! ready "$name"; do
    if ! alive "$name"; then
      echo; c_red "$name 启动失败，最后 30 行日志："; tail -n 30 "$LOG_DIR/$name.log"; return 1
    fi
    if [ "$t" -ge "$READY_TIMEOUT" ]; then echo; c_red "$name 等待超时"; return 1; fi
    sleep 5; t=$((t + 5)); printf '.'
  done
  echo; c_green "$name 已就绪：http://127.0.0.1:$(port_of "$name")/v1"
}

start_vlm() {
  build_vlm_args
  launch vlm "$VLLM_VENV" "$VLM_GPU" vllm "${VLM_ARGS[@]}"
}

start_tts() {
  build_tts_args
  launch tts "$OMNI_VENV" "$TTS_GPU" vllm "${TTS_ARGS[@]}"
}

start_asr() {
  local util="$ASR_GPU_UTIL"
  if command -v nvidia-smi >/dev/null; then
    # 与 TTS 共用显卡：按当前剩余显存自动下调占比，避免启动时报显存不足
    read -r free total < <(nvidia-smi -i "$ASR_GPU" --query-gpu=memory.free,memory.total \
                             --format=csv,noheader,nounits | tr -d ',')
    util="$(python3 -c "f,t,u=$free,$total,$ASR_GPU_UTIL; print(f'{min(u, f/t - 0.05):.2f}')")"
    echo "GPU $ASR_GPU 剩余显存 ${free}MiB / ${total}MiB，ASR 显存占比设为 $util"
    if python3 -c "import sys; sys.exit(0 if $util >= 0.12 else 1)"; then :; else
      c_red "GPU $ASR_GPU 剩余显存不足以启动 ASR，请降低 TTS 占用或改用 Qwen3-ASR-0.6B"; return 1
    fi
  fi
  build_asr_args "$util"
  launch asr "$VLLM_VENV" "$ASR_GPU" vllm "${ASR_ARGS[@]}"
}

stop_one() {
  local name="$1" p
  p="$(pid_of "$name")"
  if [ -z "$p" ] || ! kill -0 "$p" 2>/dev/null; then echo "$name 未运行"; rm -f "$RUN_DIR/$name.pid"; return; fi
  echo "停止 $name (pid $p)"
  kill -TERM -- "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null
  for _ in $(seq 1 30); do kill -0 "$p" 2>/dev/null || break; sleep 1; done
  kill -0 "$p" 2>/dev/null && { c_yel "强制结束 $name"; kill -KILL -- "-$p" 2>/dev/null || kill -KILL "$p"; }
  rm -f "$RUN_DIR/$name.pid"
}

status() {
  for n in vlm asr tts; do
    if ready "$n"; then c_green "$n  运行中  端口 $(port_of "$n")  pid $(pid_of "$n")"
    elif alive "$n"; then c_yel "$n  启动中  端口 $(port_of "$n")  pid $(pid_of "$n")（tail -f logs/$n.log 查看进度）"
    else c_red "$n  未运行"; fi
  done
  command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu \
    --format=csv
}

cmd="${1:-status}"; target="${2:-all}"
case "$cmd" in
  start)
    case "$target" in
      all)
        start_vlm; start_tts
        wait_ready tts || exit 1
        start_asr || exit 1
        wait_ready asr || exit 1
        wait_ready vlm || exit 1
        c_green "全部模型服务已就绪。下一步：python scripts/check_models.py -c config.yaml" ;;
      vlm) start_vlm && wait_ready vlm ;;
      asr) start_asr && wait_ready asr ;;
      tts) start_tts && wait_ready tts ;;
      *) echo "未知服务：$target"; exit 1 ;;
    esac ;;
  stop)
    if [ "$target" = all ]; then for n in asr tts vlm; do stop_one "$n"; done; else stop_one "$target"; fi ;;
  restart)
    "$0" stop "$target"; "$0" start "$target" ;;
  status) status ;;
  logs) tail -n 100 -f "$LOG_DIR/${target/all/vlm}.log" ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
