#!/bin/bash
# 保活脚本：只拉起没有在监听的端口，重复执行无副作用（可安全地挂 cron）
#   cron: */5 * * * * ~/vod-research/backend/start-all.sh >/dev/null 2>&1
exec python3 "$(dirname "$0")/vodctl.py" ensure "${1:-all}"
