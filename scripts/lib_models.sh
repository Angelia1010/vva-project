# 模型服务启动参数的唯一来源：models.sh（手动启动）和 install_systemd.sh（开机自启）都用这里拼出的参数，
# 保证两种方式完全一致。依赖 models.env 中的变量，由调用方先 source models.env 再 source 本文件。
#
# 调用后得到数组：VLM_ARGS / TTS_ARGS / ASR_ARGS（不含 vllm 可执行文件本身）

MODEL_HOST="${MODEL_HOST:-127.0.0.1}"
export CUDA_DEVICE_ORDER="${CUDA_DEVICE_ORDER:-PCI_BUS_ID}"   # 显卡编号与 nvidia-smi 一致

# models.env 里的 EXTRA_ENV（空格分隔的 KEY=VALUE）会传给所有模型服务，如 EXTRA_ENV="NCCL_P2P_DISABLE=1"
apply_extra_env() {
  local kv
  for kv in ${EXTRA_ENV:-}; do export "${kv?}"; done
}

gpu_count() {   # gpu_count "0,1" -> 2
  local IFS=,; set -- $1; echo $#
}

build_vlm_args() {
  local tp
  tp="$(gpu_count "$VLM_GPU")"
  VLM_ARGS=(serve "$VLM_MODEL" --served-model-name "$VLM_NAME"
            --host "$MODEL_HOST" --port "$VLM_PORT"
            --max-model-len "$VLM_MAX_LEN"
            --limit-mm-per-prompt "{\"image\": $VLM_MAX_IMAGES, \"video\": 0}"
            --gpu-memory-utilization "$VLM_GPU_UTIL"
            --enable-auto-tool-choice --tool-call-parser "${VLM_TOOL_PARSER:-hermes}")
  [ "$tp" -gt 1 ] && VLM_ARGS+=(--tensor-parallel-size "$tp")
  [ -n "${VLM_REASONING_PARSER:-}" ] && VLM_ARGS+=(--reasoning-parser "$VLM_REASONING_PARSER")
  [ -n "${VLM_SPECULATIVE:-}" ] && VLM_ARGS+=(--speculative-config "$VLM_SPECULATIVE")
  # shellcheck disable=SC2206
  [ -n "${VLM_EXTRA_ARGS:-}" ] && VLM_ARGS+=($VLM_EXTRA_ARGS)
  return 0
}

build_tts_args() {
  TTS_ARGS=(serve "$TTS_MODEL" --omni --served-model-name "$TTS_NAME"
            --host "$MODEL_HOST" --port "$TTS_PORT" --trust-remote-code)
  # --enforce-eager 关闭 CUDA graph：启动更稳，但合成首包明显变慢。TTS_ENFORCE_EAGER=0 去掉它来提速
  [ "${TTS_ENFORCE_EAGER:-1}" = "1" ] && TTS_ARGS+=(--enforce-eager)
  [ -n "${TTS_GPU_UTIL:-}" ] && TTS_ARGS+=(--gpu-memory-utilization "$TTS_GPU_UTIL")
  # shellcheck disable=SC2206
  [ -n "${TTS_EXTRA_ARGS:-}" ] && TTS_ARGS+=($TTS_EXTRA_ARGS)
  return 0
}

build_asr_args() {   # build_asr_args <显存占比>
  ASR_ARGS=(serve "$ASR_MODEL" --served-model-name "$ASR_NAME"
            --host "$MODEL_HOST" --port "$ASR_PORT"
            --gpu-memory-utilization "${1:-$ASR_GPU_UTIL}" --max-model-len 4096)
  # shellcheck disable=SC2206
  [ -n "${ASR_EXTRA_ARGS:-}" ] && ASR_ARGS+=($ASR_EXTRA_ARGS)
  return 0
}

# 把参数数组渲染成 systemd ExecStart 可用的一行（含空格或引号的参数用单引号包起来）
render_args() {
  local out="" a
  for a in "$@"; do
    if [[ "$a" =~ [[:space:]\"\{] ]]; then out+=" '$a'"; else out+=" $a"; fi
  done
  echo "${out# }"
}
