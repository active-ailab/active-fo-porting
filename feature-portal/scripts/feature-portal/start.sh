#!/usr/bin/env bash
# 功能移植工作台 —— 一键启动
# 用法:
#   ./start.sh              # 默认端口 8770
#   ./start.sh --port 9000  # 指定端口
#   ./start.sh --host 127.0.0.1
set -u

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "找不到 python3，请先安装：sudo apt install -y python3"
  exit 1
fi

WS=$(cd ../.. && pwd)

echo "------------------------------------------------------------"
echo " python : $($PY -V 2>&1)"
echo " 工作区 : $WS"
echo "------------------------------------------------------------"

exec "$PY" server.py --workspace "$WS" "$@"
