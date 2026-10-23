#!/usr/bin/env bash
# 在 WSL 里启动功能移植工作台。
# 用法: bash run.sh [端口] [无心跳后自动退出秒数]
# 端口默认 8770，--idle-exit 默认 8（页面关闭后自动退出）。
set -u

cd "$(dirname "$0")" || exit 1

PORT="${1:-8770}"
IDLE="${2:-8}"

LOG="$PWD/feature-portal-server.log"
exec >>"$LOG" 2>&1
printf '\n[%s] run.sh pid=%s directory=%s port=%s idle=%s\n' "$(date -Is)" "$$" "$PWD" "$PORT" "$IDLE"
command -v python3 || exit 127

exec python3 -u "$PWD/server.py" --port "$PORT" --idle-exit "$IDLE"
