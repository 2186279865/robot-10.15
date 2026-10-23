#!/usr/bin/env bash
# 在香橙派上启动 pi_agent（10.21 无线模式）。默认用真相机 0/2 + 真 STM32 串口。
#   真机：        ./start_agent.sh
#   无硬件自检：  ./start_agent.sh --fake-camera --fake-serial
set -u
cd "$(dirname "$0")/pi" || exit 1

pkill -f pi_agent.py 2>/dev/null
sleep 0.4

ARGS=("$@")
if [ ${#ARGS[@]} -eq 0 ]; then
  ARGS=(--cam-index 0 --cam-index 2)
fi

setsid nohup python3 pi_agent.py "${ARGS[@]}" \
  --img-port 5057 --ser-port 5058 --discovery-port 5056 \
  > ~/pi_agent.log 2>&1 < /dev/null &
sleep 2
echo "--- ps ---"; pgrep -af pi_agent.py
echo "--- ports ---"; ss -tlnp 2>/dev/null | grep -E '5057|5058'; ss -ulnp 2>/dev/null | grep 5056
echo "--- log ---"; tail -20 ~/pi_agent.log
