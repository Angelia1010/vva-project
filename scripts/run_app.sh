#!/usr/bin/env bash
# 启动智能体服务（浏览器测试模式）。存在 certs/ 证书时自动启用 HTTPS。
#   scripts/run_app.sh              前台运行
#   scripts/run_app.sh --no-audio   只用文字（尚未部署 ASR/TTS 时）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
APP_VENV="${APP_VENV:-$ROOT/.venv}"
[ -f "$APP_VENV/bin/activate" ] && source "$APP_VENV/bin/activate"
CONFIG="${CONFIG:-config.yaml}"
[ -f "$CONFIG" ] || { echo "缺少 $CONFIG，请先执行：cp config.2x3090.yaml config.yaml"; exit 1; }
ARGS=(-c "$CONFIG" --browser)
if [ -f certs/cert.pem ] && [ -f certs/key.pem ]; then
  ARGS+=(--ssl-cert certs/cert.pem --ssl-key certs/key.pem)
fi
exec python main.py "${ARGS[@]}" "$@"
