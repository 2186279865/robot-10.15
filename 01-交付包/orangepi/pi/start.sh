#!/usr/bin/env bash
# 10.21/pi/start.sh —— 前台启动 pi_agent（Ctrl+C 优雅退出）
#   ./start.sh                      # 读 config.json
#   ./start.sh --fake-camera        # 没有相机时联调
#   ./start.sh --cam-index 0 --cam-index 2
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1
exec python3 pi_agent.py "$@"
