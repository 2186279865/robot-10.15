#!/bin/sh
# start_display.sh —— 派端开机自启入口（autostart 里调它）
#   1) 关屏保、隐藏鼠标
#   2) 确保 pi_ctl.py（上位机「重启派端」用的遥控口 5062）在跑
#   3) 杀掉旧的屏幕程序，再起一个新的（幂等，不会双开）
set -u
BASE="$(cd "$(dirname "$0")" && pwd)"
export DISPLAY="${DISPLAY:-:0}"

xset s off 2>/dev/null || true
xset -dpms 2>/dev/null || true
xset s noblank 2>/dev/null || true

# 遥控口：只在没跑时拉起（崩溃后由本脚本或重启兜底）
if ! pgrep -f 'pi_ctl[.]py' >/dev/null 2>&1; then
    ( setsid python3 -u "$BASE/pi_ctl.py" --port 5062 \
        >/tmp/pi_ctl.out 2>&1 </dev/null & )
fi

pkill -f 'pi_display[.]py' 2>/dev/null || true
sleep 0.3

# 桌面组件会挡住全屏界面，一并关掉（XFCE）
pkill xfce4-panel 2>/dev/null || true
pkill xfdesktop 2>/dev/null || true
pkill xfce4-power-manager 2>/dev/null || true
pkill xfce4-notifyd 2>/dev/null || true

# 鼠标静止 0.3 s 自动隐藏
if command -v unclutter >/dev/null 2>&1; then
    unclutter -idle 0.3 -root >/dev/null 2>&1 &
fi

exec python3 -u "$BASE/pi_display.py" --port 5059 --theme light
