#!/usr/bin/env bash
# 生成自签名 HTTPS 证书，用于从其他电脑打开浏览器测试页（浏览器要求 HTTPS 才能用摄像头/麦克风）。
#   scripts/gen_cert.sh            自动使用本机第一个 IP
#   scripts/gen_cert.sh 192.168.1.20
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IP="${1:-$(hostname -I | awk '{print $1}')}"
mkdir -p "$ROOT/certs"
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout "$ROOT/certs/key.pem" -out "$ROOT/certs/cert.pem" \
  -subj "/CN=$IP" \
  -addext "subjectAltName=IP:$IP,IP:127.0.0.1,DNS:localhost" 2>/dev/null
chmod 600 "$ROOT/certs/key.pem"
echo "已生成 certs/cert.pem 与 certs/key.pem（IP: $IP）"
echo "启动：python main.py -c config.yaml --browser --ssl-cert certs/cert.pem --ssl-key certs/key.pem"
echo "访问：https://$IP:8000/browser（自签名证书会提示不安全，选择“继续访问”即可）"
