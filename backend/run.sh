#!/bin/bash
# 一键重启换源后端（与 DSH/SSH 解耦：setsid 拉起，挂到 init 下）
#   用法: ssh xc171 ~/vod-research/backend/run.sh            # 两个都重启
#         ssh xc171 ~/vod-research/backend/run.sh 18080      # 只重启主后端
#         ssh xc171 ~/vod-research/backend/run.sh 18766      # 只重启 SPA 宿主
# 真实逻辑在 vodctl.py（按进程 cwd 认领，不会像 pkill 那样把同名 server.py 一起杀掉）
exec python3 "$(dirname "$0")/vodctl.py" restart "${1:-all}"
