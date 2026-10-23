#!/usr/bin/env bash
# 10.21/pi/install.sh —— 树莓派端一键安装（apt 优先，失败再 pip）
#
#   bash install.sh              # 装依赖 + 装 systemd 自启并立即启动
#   bash install.sh --no-service # 只装依赖，不碰 systemd
#   bash install.sh --no-apt     # 跳过 apt，直接 pip
#
# 完成后请注销重登一次（dialout 组生效），再运行 ./start.sh 看是否正常。
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1

INSTALL_SERVICE=1
USE_APT=1
for arg in "$@"; do
  case "$arg" in
    --no-service) INSTALL_SERVICE=0 ;;
    --no-apt)     USE_APT=0 ;;
    -h|--help)    sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[install]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  command -v sudo >/dev/null 2>&1 || die "需要 root 或 sudo"
  SUDO="sudo"
fi

# ---------------------------------------------------------------- 环境检查
if ! command -v python3 >/dev/null 2>&1; then
  die "没找到 python3，请先装系统（Raspberry Pi OS 自带）"
fi
log "python: $(python3 -V 2>&1)  架构: $(uname -m)"

PY_OK=$(python3 - <<'PY' 2>/dev/null
try:
    import cv2, numpy
    print('cv2=%s numpy=%s' % (cv2.__version__, numpy.__version__))
except Exception as exc:
    print('missing: %s' % exc)
PY
)
log "当前 python3 图像库状态：$PY_OK"

# ---------------------------------------------------------------- 依赖安装
APT_OK=0
if [ "$USE_APT" -eq 1 ]; then
  log "尝试 apt 安装（预编译，推荐）…"
  if $SUDO apt-get install -y python3-opencv python3-numpy python3-serial; then
    APT_OK=1
    log "apt 安装成功"
  else
    warn "apt 安装失败（可能源里没有包 / 没联网），改用 pip"
  fi
else
  log "按参数跳过 apt"
fi

if [ "$APT_OK" -eq 0 ]; then
  log "使用 pip 安装 requirements-pi.txt …"
  if ! python3 -m pip --version >/dev/null 2>&1; then
    log "先装 pip："
    $SUDO apt-get install -y python3-pip || die "装 python3-pip 失败"
  fi
  PY_MAJOR_MINOR=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
  PIP_FLAGS=""
  # Debian 12 / Raspberry Pi OS bookworm 起 PEP 668 会拦 pip
  if python3 -c "import sysconfig,os;raise SystemExit(0 if os.path.exists(os.path.join(sysconfig.get_path('stdlib'),'EXTERNALLY-MANAGED')) else 1)" 2>/dev/null; then
    PIP_FLAGS="--break-system-packages"
  fi
  python3 -m pip install $PIP_FLAGS -r requirements-pi.txt \
    || die "pip 安装失败：请检查网络，或手动 apt install python3-opencv python3-numpy python3-serial"
  log "pip 安装完成（python $PY_MAJOR_MINOR）"
fi

# ---------------------------------------------------------------- 权限/调优
CURRENT_USER="${SUDO_USER:-$(id -un)}"
if id -nG "$CURRENT_USER" 2>/dev/null | grep -qw dialout; then
  log "$CURRENT_USER 已在 dialout 组"
else
  warn "把 $CURRENT_USER 加入 dialout 组（访问 /dev/ttyACM* 必需）"
  $SUDO usermod -aG dialout "$CURRENT_USER" || warn "usermod 失败，请手动执行"
  warn "→ 需要注销并重新登录（或 reboot）才生效"
fi

# 关闭 WiFi 省电：省电模式下 WiFi 会周期性休眠，图传延迟忽高忽低
if command -v iw >/dev/null 2>&1 && iw dev wlan0 info >/dev/null 2>&1; then
  $SUDO iw dev wlan0 set power_save off >/dev/null 2>&1 \
    && log "已临时关闭 WiFi 省电（重启后失效，永久生效见 README）" \
    || warn "关闭 WiFi 省电失败（可忽略）"
fi

cat <<'EOF'

提示（可选，提高现场稳定性）：
  1) 固定 IP：sudo nmcli con mod "preconfigured" ipv4.method manual \
       ipv4.addresses 192.168.1.50/24 ipv4.gateway 192.168.1.1 ipv4.dns 223.5.5.5
     （连接名用 sudo nmcli -t -f NAME con show 查；笔记本端就能写死 IP）
  2) 永久关闭 WiFi 省电：
     echo -e "[connection]\nwifi.powersave = 2" | sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf
     sudo systemctl restart NetworkManager
  3) 串口设备权限检查：ls -l /dev/ttyACM0   （应显示 crw-rw---- root dialout）

EOF

# ---------------------------------------------------------------- systemd
# 注意：不能直接拷 robotpi.service —— 里面写死的是「树莓派 + 用户 pi +
# /home/pi/10.21/pi」。换机器（香橙派、用户名 orangepi、目录 ~/robot1021/pi）
# 就会启动失败，而且 systemd 只会把错误留在 journal 里，现场看起来就是
# 「上位机点了连接没反应」。所以这里按当前机器现场生成。
if [ "$INSTALL_SERVICE" -eq 1 ]; then
  SERVICE_USER="$(id -un)"
  SERVICE_GROUP="$(id -gn)"
  # 相机索引用「出现顺序」，不是设备号；两边都是 0/2 时最稳。
  # 想改：PI_CAM_ARGS="--cam-index 0 --cam-index 4" bash install.sh
  SERVICE_ARGS="${PI_CAM_ARGS:---cam-index 0 --cam-index 2}"
  log "安装 systemd 服务 robotpi.service（用户 $SERVICE_USER，目录 $HERE）"
  TMP_UNIT="$(mktemp)"
  cat > "$TMP_UNIT" <<UNIT
[Unit]
Description=Robot Pi Agent (image stream + serial bridge + UDP discovery)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
SupplementaryGroups=dialout
WorkingDirectory=$HERE
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 $HERE/pi_agent.py $SERVICE_ARGS
Restart=always
RestartSec=3
KillSignal=SIGTERM
TimeoutStopSec=10
Nice=0

[Install]
WantedBy=multi-user.target
UNIT
  $SUDO cp "$TMP_UNIT" /etc/systemd/system/robotpi.service || die "写服务文件失败"
  rm -f "$TMP_UNIT"
  # 手动起的 agent 会和 service 抢 5056/5057/5058，先停掉
  pkill -f "pi_agent[.]py" 2>/dev/null || true
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable robotpi.service
  if $SUDO systemctl restart robotpi.service; then
    log "服务已启动并设为开机自启：systemctl status robotpi --no-pager -l"
  else
    warn "服务启动失败，用下面命令看原因：journalctl -u robotpi -n 50 --no-pager"
  fi
else
  log "按参数跳过 systemd 安装"
fi

log "安装流程结束。手动运行：./start.sh   （Ctrl+C 退出）"
log "开机自启：systemctl status robotpi --no-pager -l"
