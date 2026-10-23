#!/bin/bash
# Start pi_agent on the Orange Pi 4 Pro with both USB cameras.
# Safe to re-run: kills the previous instance first.
set -u
cd "$HOME/robot1021/pi" || exit 1

pkill -f 'pi_agent.py' >/dev/null 2>&1
sleep 1

setsid nohup python3 pi_agent.py \
    --cam-index 0 --cam-index 2 \
    --img-port 5057 --ser-port 5058 --discovery-port 5056 \
    > "$HOME/pi_agent.log" 2>&1 < /dev/null &
echo "launched pid $!"
sleep 4
echo "--- ps ---"
ps -eo pid,etime,cmd | grep 'pi_agent.py' | grep -v grep
echo "--- tcp ---"
ss -ltn | grep -E '5057|5058'
echo "--- udp ---"
ss -lun | grep 5056
echo "--- log tail ---"
tail -25 "$HOME/pi_agent.log"
