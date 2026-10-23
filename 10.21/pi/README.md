# 树莓派端服务（pi_agent）部署与联调手册

本目录是 2027 工创赛智能分拣机器人的**树莓派端**：笔记本（Windows 上位机）通过 WiFi 连到同一路由器，
由笔记本**主动连**树莓派的两个 TCP 端口：

```
              Windows 笔记本（上位机 + SAM 视觉）             树莓派 4B/5（本目录）
  +--------------------------------------------------+      +------------------------------------+
  | vision/link.py  ──TCP 5057 请求 9 字节/收 JPEG──▶ |─────▶| pi_agent.py  ImageServer           |
  |                                                  |      |   CameraWorker × N（V4L2 / 假画面） |
  | host/serial_link.py ──TCP 5058 字节级透传───────▶ |─────▶|   SerialBridge ──▶ STM32 USB CDC   |
  |                                                  |      |                    (ttyACM0)        |
  | 启动时发 UDP 广播 ROBOTPI? ──▶ 5056 收应答        |◀────▶|   DiscoveryServer                  |
  +--------------------------------------------------+      +------------------------------------+
```

要点：

- **不推流**。笔记本要一帧就发一个 9 字节请求，树莓派回一帧 JPEG；`w/h=0` 表示"用派端配置尺寸"。
- **串口协议不解析**。笔记本把已经算好校验和的整行字节原样发来，`SerialBridge` 原样写进串口；
  STM32 的回包原样回传。协议格式以 `../common/wire.py` 与 `../host/protocol.py` 为唯一标准（`10.21/pi/` 内**没有**协议副本）。
- 依赖只有：Python 3 标准库 + `opencv-python-headless`（或 apt 的 `python3-opencv`）+ `numpy` + `pyserial`。
  pyserial 是**可选导入**：没装也能跑 `--fake-camera`/`--fake-serial` 联调。

---

## 1. 端口与报文一览

| 端口 | 用途 | 方向 | 说明 |
| --- | --- | --- | --- |
| `5057/tcp` | 图传（拉模式） | 笔记本 → 派 | 请求 9B，回 `IM`+24B 头+JPEG 或 `ER`+6B 头+错误文本 |
| `5058/tcp` | 串口桥 | 双向 | 笔记本→串口、串口→笔记本原样字节流 |
| `5056/udp` | 发现 | 广播 | 收 `ROBOTPI?`，回 `ROBOTPI!ip|img|ser|name` |

图传请求（`<2sHHHB`，共 9 字节）：

```
b'RQ' | cam_id(u16) | width(u16) | height(u16) | quality(u8)
```

- `cam_id` = `--cam-index` 出现的序号（第 1 个 `--cam-index` 是 cam 0）。
- `width/height/quality` 任一为 0 → 用派端配置（`--width/--height/--quality`）。
- 相机没配置/打不开 → 回 `ER` 错误包（`E_NO_CAMERA=1`），**连接不断**，可以继续请求。
- JPEG 质量上限 100，请求值会被 clamp。

错误包（`ER`，6 字节头 + 文本体）：

```
b'ER' | code(u16) | n(u16) | msg(n 字节, utf-8，真正的错误细节)
```

> **笔记本端容易踩的坑**：`wire.parse_error_header()` 返回的 `text` 是 `wire.ERROR_TEXT` 里的固定文案
> （如“相机未打开”），具体细节（如“相机 1 未配置（可选：0）”）在 `n` 字节 body 里。
> 收包必须**先读 2 字节 magic 再分头长**：`IM` → `IMG_HDR_LEN=24`，`ER` → `ERR_HDR_LEN=6`；
> 一律按 24 字节读会多吞 18 字节、然后等 socket 超时（现象是“取帧失败：timed out”）。
> 界面上建议 `text + '：' + body.decode('utf-8')` 一起显示，两块信息都不丢。参考实现见 `tests/selftest_client.py`。

启动成功后日志里会有一行可直接复制给笔记本：

```
pi ready: img=192.168.1.50:5057 ser=192.168.1.50:5058 name=robot-pi
```

## 2. 树莓派准备（从零开始）

1. 烧录系统：Raspberry Pi OS（64 位，`Bookworm` 或更新；Lite 版就够，我们不开 HDMI 界面）。
   用 Raspberry Pi Imager 的高级设置直接写好：主机名、用户名 `pi`、WiFi 国家/SSID/密码、开启 SSH。
2. **必须和笔记本接同一个路由器**（同一网段）。建议在路由器上给派做 MAC 绑定（固定 IP），
   或在派上配静态地址——否则每次开机 IP 变了笔记本就得重新找。
3. 首次开机后 `ssh pi@<派IP>` 登录，先更新一次：`sudo apt update && sudo apt upgrade -y`。
4. 打开相机权限（默认已开，可确认）：

   ```bash
   sudo raspi-config nonint do_camera 0        # 0 = 打开（仅 CSI 相机需要；USB 相机不需要）
   ls /dev/video*                              # 有 /dev/video0、/dev/video1 ... 说明驱动在
   ```

5. **找相机**（USB 相机插上后重插一次，确保供电）：

   ```bash
   sudo apt install -y v4l-utils
   v4l2-ctl --list-devices                     # 列出每个相机对应的 /dev/videoN
   v4l2-ctl -d /dev/video0 --list-formats-ext  # 看支持的分辨率/帧率/格式
   ```

   注意：一个 USB 相机常常占**两个** `/dev/videoN`（一个采集、一个元数据），
   用 `v4l2-ctl --list-devices` 看名字区分；能出图的一般是编号小的那个。
   **推荐用 USB 相机**：CSI 排线相机走 libcamera，OpenCV 4.x 默认打不开（需要额外装 `python3-libcamera` 并走 picamera2 路径，本项目不用）。

## 3. 拷贝并安装

```bash
# 在笔记本上（Windows Git Bash / PowerShell 均可，以 scp 为例）
scp -r 10.21 pi@192.168.1.50:/home/pi/

# 在树莓派上
cd /home/pi/10.21/pi
chmod +x install.sh start.sh
./install.sh                 # apt 优先，失败自动退回 pip --break-system-packages
# 可选：./install.sh --no-service   （只装依赖、不装开机自启）
```

`install.sh` 会做这些事，任何一步失败都会打印手工命令：

1. 检查 `python3`（3.9+）；
2. `sudo apt install -y python3-opencv python3-numpy python3-serial`（免编译，最快）；
3. apt 装不上就 `python3 -m pip install --break-system-packages -r requirements-pi.txt`
   （Bookworm 的 pip 有 PEP 668 保护，所以必须带 `--break-system-packages`，或者用 venv）；
4. `sudo usermod -aG dialout $USER` —— **加完组要重新登录（或重启）才生效**，否则串口 `Permission denied`；
5. 关闭 WiFi 省电：`sudo iw dev wlan0 set power_save off`（防止长时间空闲后 WiFi 假死，串口桥掉线）；
6. 打印可选优化：永久关省电、静态 IP、udev 固定串口名。

> 依赖说明：`opencv-python-headless` 是**没有 GUI 的** OpenCV，树莓派没有桌面也够用；
> 不要再装 `opencv-python`（会拉一堆 Qt/X11 依赖）。若 apt 装了 `python3-opencv`，pip 那行可以跳过。

## 4. 运行

```bash
cd /home/pi/10.21/pi
./start.sh                                   # 全默认：cam /dev/video0，1280x720@30，q=80
./start.sh --cam-index 0 --cam-index 2       # 两路 USB 相机：cam0=/dev/video0, cam1=/dev/video2
./start.sh --width 640 --height 480 --fps 30 --quality 70   # 带宽/CPU 吃紧时的保守配置
```

看到下面几行就说明服务全起来了：

```
[2026-10-21 22:02:48.644] [img] 图传服务已监听 0.0.0.0:15057（相机 0）
[2026-10-21 22:02:48.644] [ser] 串口桥已监听 0.0.0.0:15058（多客户端允许并发，串口回包广播）
[2026-10-21 22:02:48.644] [ready] pi ready: img=192.168.1.50:5057 ser=192.168.1.50:5058 name=robot-pi
```

首次上电建议先分步验证：

```bash
# 1) 没有相机/没有 STM32 时，用假数据打通网络（笔记本上干这个最容易排查）
./start.sh --fake-camera --fake-serial

# 2) 真相机 + 假固件（先确认图传，再上串口）
./start.sh --fake-serial

# 3) 全真：相机 + /dev/ttyACM0（STM32 插上后 ls /dev/ttyACM* 确认设备名）
./start.sh --serial /dev/ttyACM0
```

插着 STM32 但设备名是 `ttyUSB0` 也正常（取决于板子 USB 芯片），`--serial /dev/ttyUSB0` 即可；
想一劳永逸可以加 udev 规则固定名字（见第 8 节）。

### 4.1 常用参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--host` | `0.0.0.0` | 监听地址，`0.0.0.0` = 所有网卡 |
| `--img-port` | `5057` | 图传 TCP 端口 |
| `--ser-port` | `5058` | 串口桥 TCP 端口 |
| `--discovery-port` | `5056` | UDP 发现端口 |
| `--serial` | `/dev/ttyACM0` | 串口路径；Windows 上必须是 `COMx` |
| `--baud` | `115200` | 8N1，STM32 CDC 固定不变 |
| `--cam-index` | `0` | 相机设备号，可给多次（`--cam-index 0 --cam-index 2`） |
| `--width / --height` | `1280 / 720` | 采集尺寸 |
| `--fps` | `30` | 采集帧率 |
| `--quality` | `80` | 默认 JPEG 质量 1..100 |
| `--name` | `robot-pi` | 发现应答里的机器名 |
| `--fake-camera` | 关 | 用 numpy 合成画面代替真相机（联调用） |
| `--fake-serial` | 关 | 进程内"假 STM32"，不用串口（联调用） |
| `--no-camera` | 关 | 不启动图传服务 |
| `--no-serial` | 关 | 不启动串口桥 |
| `--no-discovery` | 关 | 不启动 UDP 发现 |
| `--log-file <path>` | 无 | 日志同时追加写入文件（systemd 下建议不写，journal 已收） |
| `--config <path>` | `config.json` | 配置文件路径，相对路径按**启动时工作目录**解析 |
| `--exit-after <秒>` | 0 | 联调/CI 用：N 秒后自动优雅退出（0 = 一直跑） |

优先级：**命令行 > config.json > 内置默认**。`config.json` 示例：

```json
{
  "host": "0.0.0.0",
  "img_port": 5057,
  "ser_port": 5058,
  "serial": "/dev/ttyACM0",
  "cameras": [0, 2],
  "width": 1280,
  "height": 720,
  "fps": 30,
  "quality": 80,
  "name": "robot-pi"
}
```

`cameras` 里每一项可以是整数（设备号）或对象 `{"index": 0, "width": 640, "height": 480, "fps": 30}`，
**数组下标就是 cam id**。命令行一旦给了 `--cam-index`，就完全覆盖 `cameras`。

## 5. 开机自启（systemd）

`install.sh` 已经帮你装好了；手工安装是：

```bash
sudo cp /home/pi/10.21/pi/robotpi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now robotpi
systemctl status robotpi            # active (running) 即成功
journalctl -u robotpi -f            # 实时看日志（Ctrl+C 退出）
sudo systemctl restart robotpi      # 改完 config.json 记得重启
```

`robotpi.service` 关键内容：

```ini
WorkingDirectory=/home/pi/10.21/pi
ExecStart=/usr/bin/python3 pi_agent.py
Restart=always
RestartSec=3
```

要改默认参数（比如两路相机），**不要改 service 文件**，改 `config.json` 即可（相对路径以 WorkingDirectory 为基准）。
非要加 CLI 参数就编辑 `ExecStart`，例如 `ExecStart=/usr/bin/python3 pi_agent.py --cam-index 0 --cam-index 2`，
然后 `sudo systemctl daemon-reload && sudo systemctl restart robotpi`。

如果 `/home/pi/10.21` 不是这个路径，service 里的 `WorkingDirectory` 和 `ExecStart` 两处都要改。

## 6. 防火墙

树莓派默认不装 ufw。装了的话放行三个端口（UDP 发现是广播，笔记本看不到应答通常就是它被挡了）：

```bash
sudo ufw allow 5057/tcp comment 'pi img'
sudo ufw allow 5058/tcp comment 'pi serial'
sudo ufw allow 5056/udp comment 'pi discovery'
# 更严格：只允许本网段
# sudo ufw allow from 192.168.1.0/24 to any port 5057 proto tcp
sudo ufw reload
```

笔记本侧 Windows Defender 也要允许 Python 出站（默认允许），若笔记本收不到图，
先在**派上**自测：`python3 tests/selftest_client.py --img-port 5057 --ser-port 5058`（本机回环，不经过网络）。

## 7. 笔记本侧联调（Windows）

`tests/selftest_client.py` 只依赖标准库 + cv2，不 import 任何 host 代码：

```powershell
# 全量自测（图传 A/B/C、串口桥 D/E/F/G、UDP 发现 H）
python 10.21/pi/tests/selftest_client.py --img-port 5057 --ser-port 5058 --discovery-port 5056 `
    --expect-width 1280 --expect-height 720
# 只测图传（--discovery-port 0 表示跳过 H）
python 10.21/pi/tests/selftest_client.py
```

它会打印每一项 PASS/FAIL 并以退出码 0/1 结束；"回包"那几行就是 STM32 侧原样返回的协议行。

`tests/test_host_protocol.py` 更进一步：它 `import` 真实的 `../host/protocol.py` 与 `../host/host_client.py`，
用真实上位机客户端把 hello/ping/motor_*/gripper/param_*/tele/stop_all/锁存/去重 全跑一遍。
**必须在仓库根目录能 import 到 host 时才可用**（它自己会 sys.path 插入 `10.21/host`）：

```powershell
python 10.21/pi/tests/test_host_protocol.py --ser-port 5058
```

> 联调时树莓派用 5057/5058，若同时跑多个实例（或端口被占），用 `--img-port 15057 --ser-port 15058` 之类的 15xxx 端口，
> 测试脚本也传同样的端口即可。

## 8. 多客户端与看门狗（行为约定）

- **串口桥允许并发**：多个笔记本客户端可同时连 5058（调试时方便开两个窗口）。
  笔记本→串口方向加写锁**串行**发送（不会把两帧字节交叉写进串口）；
  串口→笔记本方向把回包**广播**给所有在线客户端。
- **看门狗**：连续 `1500 ms` 没收到**任何**笔记本字节，就向串口写一次"四轴停车帧"
  （`wire.STOP_FRAMES`，地址 1/3/4/5 的停止帧），日志记"已护栏"。
  收到新字节立即复位；**每个空闲期只发一次**，不会刷屏。上位机进程崩了/网断了，至少车会停。
- 图传同一相机被多个连接并发请求时，每次都取"最新一帧"（相机采集线程持续覆盖缓存，天然丢积压），
  锁只保护取帧/缓存，不会因为编码慢而阻塞相机。

## 9. 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 启动就报 `Address already in use` | 端口被旧进程占了 | `sudo systemctl stop robotpi`；或 `ss -ltnp \| grep 505` 找进程；联调换 15xxx 端口 |
| 图传回 `E_NO_CAMERA` / "相机 0 未打开" | 设备号不对、相机没插好、被别的程序占着 | `ls /dev/video*` + `v4l2-ctl --list-devices` 重新确认 `--cam-index`；拔插相机；关掉占用它的程序 |
| 相机能开但画面全黑/花屏 | USB 供电不足/带宽不够 | 换 USB3 口或带供电 HUB；降到 640x480@30；不要同时开两路 1280x720（见风险） |
| 日志刷"相机 N 连续抓帧失败，重开设备" | 两路相机挤在同一个 USB2 hub，且像素格式是未压缩的 YUYV（720p 单路 ≈55 MB/s） | `pi_agent.py` 自 2026-10-23 起在 Linux 上 `_open_capture()` 先设 `CAP_PROP_FOURCC=MJPG`（压缩后约 1/10 带宽）；若仍失败，`lsusb -t` 看两路是否同 Bus，拆到不同 USB 控制器或降分辨率 |
| 串口 `Permission denied` | 不在 `dialout` 组 | `sudo usermod -aG dialout $USER` 后**重新登录**或重启 |
| 串口 `FileNotFoundError` | 设备名不是 ttyACM0 | `ls /dev/ttyACM* /dev/ttyUSB*`，用 `--serial` 指定；或加 udev 规则 |
| 上位机连上串口桥但没回包 | STM32 没上电/程序没跑/USB 线只有供电没数据 | 用 `--fake-serial` 先验证桥；再换线/换口；`dmesg \| tail` 看 USB 枚举 |
| 笔记本连不上 5057/5058 | 不同网段、防火墙、IP 变了 | 派上 `ip a` 看 IP；`pi ready` 行里的 IP 为准；关 ufw；笔记本 `ping` 一下 |
| UDP 发现收不到应答 | 广播被防火墙/路由器隔离（AP isolation） | 直接用 `pi ready` 里的 IP；或者关掉路由器"AP 隔离"选项 |
| 跑几分钟后 WiFi 掉线/延迟暴涨 | WiFi 省电模式 | `sudo iw dev wlan0 set power_save off`；`install.sh` 已尝试关闭 |
| `ModuleNotFoundError: cv2` | 依赖没装 | `./install.sh`，或 `sudo apt install python3-opencv python3-numpy` |
| `ModuleNotFoundError: serial` | 没装 pyserial（只影响真串口） | `sudo apt install python3-serial` 或 pip；`--fake-serial` 不受影响 |
| systemd 反复重启 | 启动即崩（端口/配置/相机异常） | `journalctl -u robotpi -n 100 --no-pager` 看第一段报错；`Restart=always` 会一直重试 |
| 日志中文乱码 | 终端编码不是 UTF-8 | 日志文件本身是 UTF-8；Windows 上重定向到文件时程序会自动切 UTF-8 |

想给串口固定名字（避免 ttyACM0/ttyUSB0 漂移）：

```
# /etc/udev/rules.d/99-stm32.rules
SUBSYSTEM=="tty", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5740", SYMLINK+="stm32"
```

然后 `--serial /dev/stm32`（VID/PID 用 `lsusb` 查，STM32 虚拟串口常见 0483:5740）。

## 10. 为什么这样设计 / 已知风险

- **拉模式**：笔记本要帧才给帧，网络抖动只会掉帧不会积压，视觉侧的"最新帧"语义天然成立；
  代价是每次请求一个 RTT，跨 WiFi 约 2~8 ms，30fps 完全够。
- **JPEG 软编码**：树莓派 4B 单核跑 1280x720 q=80 约 6~10 ms/帧，两路相机同时请求就要 12~20 ms，
  30fps 会吃紧。**建议两路都用 640x480，或降低 fps/quality**；树莓派 5 会宽裕得多。
- **USB 带宽**：USB2 口跑一路 1280x720@30（YUYV）已接近上限，两路必须用 USB3 口或降到 640x480。
- **WiFi 带宽**：1280x720 q=80 约 120~250 KB/帧，30fps 就是 4~7 MB/s，普通 WiFi 下两路会拥塞；
  视觉只在需要时拉帧，别持续满速拉。
- **串口桥不解析协议**：派端故意不认识任何命令，上位机改协议不需要动树莓派；但这也意味着
  **派端无法拦截危险指令**，唯一兜底就是 1.5 s 看门狗停车帧。
- **pyserial 可选**：没装 pyserial 时真串口模式会直接报错退出（而不是静默不工作），`--fake-serial` 不需要它。

---

## 11. 本目录文件

| 文件 | 说明 |
| --- | --- |
| `pi_agent.py` | 主程序：图传 + 串口桥 + UDP 发现（含假相机/假 STM32） |
| `config.json` | 默认参数（CLI 可覆盖） |
| `requirements-pi.txt` | pip 依赖 + apt 免编译替代方案 |
| `install.sh` | 一键装依赖、加 dialout 组、关 WiFi 省电、装 systemd |
| `start.sh` | `cd` 到脚本目录后 `exec python3 pi_agent.py "$@"` |
| `robotpi.service` | systemd 单元（Restart=always, RestartSec=3） |
| `tests/selftest_client.py` | 端到端自测：拉帧 + 协议行 + 发现（仅标准库 + cv2） |
| `tests/test_host_protocol.py` | 用真实上位机代码（host/protocol.py + host_client.py）验收假固件 |
| `README.md` | 本手册（端口/报文、部署、systemd、故障排查、设计取舍） |

联调时用 `--log-file` 生成的日志（如 `tests/_agent_test.log`）是临时产物，可随时删除，不要提交。
