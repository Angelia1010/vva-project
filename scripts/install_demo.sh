#!/usr/bin/env bash
# 安装“办公桌”测试场景：两个技能 + 一份知识库文档。
#   scripts/install_demo.sh [服务地址]      默认 https://127.0.0.1:9000
# 已存在的同名技能不会被覆盖。配置了 web.admin_token 时，先 export VVA_ADMIN_TOKEN=口令。
# 用 sh 运行时自动改用 bash（脚本用到了 bash 的语法）
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO="$ROOT/examples/demo-office"
URL="${1:-https://127.0.0.1:9000}"
SKILLS_DIR="$ROOT/skills"

echo "== 1. 复制技能到 $SKILLS_DIR"
mkdir -p "$SKILLS_DIR"
for d in "$DEMO"/skills/*/; do
  name="$(basename "$d")"
  if [ -e "$SKILLS_DIR/$name" ]; then
    echo "   已存在，跳过：$name"
  else
    cp -r "$d" "$SKILLS_DIR/$name"
    echo "   已复制：$name"
  fi
done

echo "== 2. 重启服务（技能的专用工具 tools.py 在启动时加载）"
if systemctl cat vva-app.service >/dev/null 2>&1; then
  sudo systemctl restart vva-app
else
  echo "   没有找到 vva-app 服务，请手动重启 scripts/run_app.sh 后再继续"
  read -r -p "   重启完成后按回车继续..." _
fi

echo "== 3. 等待服务就绪"
for _ in $(seq 1 60); do
  curl -ks -o /dev/null "$URL/api/capabilities" && break
  sleep 2
done

echo "== 4. 上传知识库文档"
for f in "$DEMO"/knowledge/*; do
  name="$(basename "$f")"
  if curl -ks "$URL/api/knowledge" | grep -q "\"name\": *\"$name\""; then
    echo "   已存在，跳过：$name"
    continue
  fi
  code=$(curl -ks -o /dev/null -w "%{http_code}" -X POST -H "X-Token: ${VVA_ADMIN_TOKEN:-}" \
         --data-binary "@$f" "$URL/api/knowledge?name=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$name")")
  echo "   $name → HTTP $code"
done

cat <<TIP

安装完成。接下来：
  1. 打开 $URL/skills ，在“办公桌巡查”里把“我的水杯”“工牌”的外观改成你实际的物品，
     拿着物品对准摄像头，点“截取当前画面”添加参考图
  2. 打开 $URL/browser 按 examples/demo-office/测试清单.md 逐项测试
TIP
