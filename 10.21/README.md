# 10.21 — 笔记本 + 树莓派（同 WiFi）一体化分拣系统

> 本项目是 2027 年第十届中国大学生工程实践与创新能力大赛「智能分拣」赛项的四轴抓取机器人软件。
> 本目录（`10.21/`）是**自包含**版本：把 `final/` 冻结包里能用的东西全部搬了进来，并把架构改成
> **相机 + STM32 都接在树莓派上，笔记本通过同一个 WiFi 远程取帧、远程下发运动指令**。

三句话说明它怎么工作：

```
树莓派（Linux）                          笔记本（Windows）
┌───────────────────────┐                ┌──────────────────────────────┐
│ 两路 USB 相机          │  TCP 5057 拉帧  │ 视觉页：RemoteCamera 取帧     │
│ pi_agent.py ────────────┼───────────────▶│  → 四点单应 → SAM 分割 → 融合 │
│  │                    │  （JPEG，按需）  │  → 规则分类 → 目标 u/v        │
│  └─ /dev/ttyACM0 ──────┼──TCP 5058 透传─▶│ 控制页：SocketLink 当"串口"   │
│     STM32F407 四轴     │  （字节级原样）  │  → 上位机协议行               │
└───────────────────────┘                └──────────────────────────────┘
```

- **识别放笔记本**：SAM-B 在笔记本 GPU 上 0.8–1 s/帧，派上要 20–30 s，所以派只当"眼睛的视网膜"，不当"大脑"。
- **运动指令走串口桥**：笔记本的上位机代码几乎不改，只是把 `COM3` 换成 `tcp://192.168.137.50:5058`。
- **默认行为不变**：不传 `--pi` 时就是原来的本地模式（USB 相机 + USB 串口），零回归。

---

## 1. 三种运行模式

| 模式 | 命令 | 相机 | STM32 | 用途 |
|---|---|---|---|---|
| 本地（原 final 行为） | `run.bat` | 笔记本 USB 相机 | 笔记本 USB 串口 | 单机调试 |
| **WiFi + 树莓派** | `run.bat --pi 192.168.137.50` | 派上的相机（TCP 5057） | 派上的 CDC 串口（TCP 5058） | 比赛/演示 |
| 纯模拟 | `run.bat --sim` | 无 | 模拟器 | 没硬件时练界面/流程 |

---

## 2. 快速开始

### 2.1 笔记本（Windows）

```bat
:: 第一次：建虚拟环境 + 装依赖（约 5~10 分钟，装完就不再需要网络）
setup.bat

:: 启动（本地模式）
run.bat

:: 启动（WiFi 模式，派已经跑起来的前提下）
run.bat --pi 192.168.137.50
```

启动后是 8 页签一体化窗口：**视觉 / 四轴控制 / X/Y 标定 / 舵机夹爪 / 参数 / 动作序列 / 视觉目标 / 原始日志**。

### 2.2 树莓派（Raspberry Pi OS 64-bit）

```bash
# 1) 把整个 10.21 目录拷到派上，例如 /home/pi/10.21
cd /home/pi/10.21/pi

# 2) 装依赖（apt 优先，免编译）
./install.sh

# 3) 看相机编号
v4l2-ctl --list-devices          # 或 ls /dev/video*

# 4) 启动
./start.sh --cam-index 0 --cam-index 2 --width 1280 --height 720

# 5) 开机自启（可选）
sudo cp robotpi.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now robotpi
```

派启动后会打印：`pi ready: img=192.168.137.50:5057 ser=192.168.137.50:5058 name=robot-pi`

### 2.3 两端验通（强烈建议每次开机都跑）

```powershell
# 笔记本上
python 10.21\net\find_pi.py                 # UDP 自动发现
python 10.21\net\find_pi.py 192.168.137.50 --full   # 指定 IP + 取一帧测速
```

### 2.4 一键连派（不用记 IP、不用命令行参数）

派在跑 `pi_agent.py`、两端同一 WiFi 时：

1. 双击 `10.21\run.bat`（就是平时启动的那个）；
2. 左上角端口下拉框里会多出一行 **`派 robot-pi 192.168.100.198（图传 5057 / 串口 5058）`**
   （启动后 1~2 秒后台 UDP 发现自动填进去；上一次连过的 IP 也会记住——见 `runtime/pi_last.json`）；
3. 选中它 → 点 **连接**。

连上之后**同时**发生三件事，跟连 STM32 串口一样一步到位：

- 控制通道走 `tcp://<派IP>:5058`（右上「在线」+ 固件版本）；
- 视觉自动切到**派端相机**并**自动打开双画面**（不用再去视觉页点）；
- 断线重连时自动清一次停止锁存、夹爪自动回 150° 预备位。

> 发现不到派（路由器开了 AP 隔离、或跨网段）时，直接在端口框里手打
> `tcp://192.168.100.198:5058` 再点连接，效果完全一样。

---

## 3. 目录地图

```
10.21/
├─ run.bat / setup.bat        一键启动 / 一键装依赖（纯 ASCII + CRLF，别再改坏）
├─ selftest.bat               ⚑ 一键跑全部自测（7 步，无需真机；/quick 跳过界面）
├─ main.py                    8 页一体化入口（--pi / --sim / --port / --selftest）
├─ requirements.txt           笔记本端依赖（torch + ultralytics + opencv + pyserial）
├─ common/
│  └─ wire.py                 ★ 两端唯一的线路协议实现（图传/发现/停车帧），改协议先改它
├─ host/                      上位机（原 ghr/10.15/上位机，tkinter 纯标准库）
│  ├─ protocol.py             上位机↔固件的串口协议（19 命令 / E01–E13 错误码）
│  ├─ serial_link.py          ⚑ 新增 SocketLink：tcp://IP:5058 当串口用
│  ├─ host_client.py          会话、命令、异步应答
│  ├─ pi_link.py              ★ 把「派」做成端口下拉框里的一个条目（UDP 发现 + 记住上次 IP）
│  ├─ host_gui.py             六页界面（四轴/夹爪/参数/序列/视觉目标/日志）
│  ├─ calib_tab.py            X/Y 标定页（final 独有）
│  ├─ approach_route.py       ★ 接近段示教数据（X/Y/Z 相对段、校验、重放时延估算）
│  ├─ journey.py              ★ 移动流水账（四轴全部位移，用于「原路返回初始位」）
│  ├─ sequence.py             动作序列编辑与执行
│  ├─ vision_import.py        读视觉目标 JSON（schema 5，u/v 归一化）
│  ├─ simulator.py            无硬件模拟器
│  └─ tests/                  164 个离线用例（含 pi_link、approach_route、journey；修好了搬目录后失效的 3 类路径/临时目录问题）
├─ vision/                    视觉（原 new_eye）
│  ├─ gui.py                  视觉主界面（双画面 / 四点标定 / 单次识别 / 实时分割）
│  ├─ camera.py               本地相机（Windows CAP_DSHOW）
│  ├─ remote_camera.py        ⚑ 远程相机（TCP 拉 JPEG，接口与 camera.Camera 一致）
│  ├─ camera_factory.py       ⚑ 按 ROBOT_PI / config.remote.host 自动选本地或远程
│  ├─ auto_segment.py         SAM-B 分割（输出 mask/contour/bbox/center_tray/confidence）
│  ├─ fusion.py               双摄一对一融合
│  ├─ classify.py             规则分类（形状 + 颜色 + 样本登记）
│  ├─ tray_calib.py           托盘像素 → X/Y 脉冲（二维仿射）
│  ├─ config.json             相机编号 / 四点 / 旋转 / 分类表 / remote.host
│  └─ models/sam_b.pt         SAM-B 权重（375 MB，已内置，不需要联网下载）
├─ pi/                        树莓派端
│  ├─ pi_agent.py             ★ 图传服务 + 串口桥 + UDP 发现（支持 --fake-camera/--fake-serial）
│  ├─ config.json             派端默认参数
│  ├─ install.sh / start.sh / robotpi.service / requirements-pi.txt
│  ├─ README.md               派端部署手册（烧录→装依赖→开机自启→故障）
│  └─ tests/selftest_client.py 不求笔记本也能自测派端
├─ net/
│  ├─ 组网指南.md             ★ 四种同 WiFi 方案 + 防火墙 + 离线部署 + 赛前 10 分钟清单
│  ├─ find_pi.py              发现派 + 端口连通性 + 取帧测速
│  └─ firewall-allow.ps1      管理员一键放行 5055-5059
├─ docs/                      ★ 全部文档（不依赖外部文件）
│  ├─ 网络协议-接口约定.md     ★ 图传/串口桥/发现 的字节级协议（改代码前必读）
│  ├─ 项目总README.md         原仓库总说明（硬件/编译/编码/密钥）
│  ├─ 交接文档.md             固件手交文档（引脚/协议/坑）
│  ├─ 环境复刻流程.md         换电脑复刻四层环境的步骤
│  ├─ 上位机方案.md           上位机设计与四期里程碑
│  ├─ 视觉-README.md / 视觉-交接文档.md / 视觉-标定操作手册.md
│  ├─ 新手联合开发完整教程.md / CONTRIBUTING.md
│  ├─ EMM_V5_串口协议与工程指令总结.md
│  └─ newpdftext.txt / pdftext.txt   赛题官方规则 + 命题解析
├─ tests/
│  ├─ test_net_local.py       ⚑ 端到端自测：假派 → RemoteCamera 取帧 + SocketLink 握手
│  ├─ gui_remote_smoke.py     ⚑ 真窗口 + 远程相机出图冒烟
│  ├─ wait_ports.py           ⚑ selftest 用：等端口打开/关闭
│  └─ check_vision_imports.py ⚑ selftest 用：视觉包导入 + SAM 权重检查
└─ runtime/                   界面截图、日志、标定产物（默认不进 git）
```

⚑ = 本次相对 `final/` 新增或修改；★ = 本次新写的核心文件。

---

## 4. 相对 `final/` 改了什么（为什么）

| 改动 | 文件 | 原因 |
|---|---|---|
| 新增线路协议 | `common/wire.py` | 两端共用一个实现，避免"各写一份对不上" |
| 新增派端服务 | `pi/pi_agent.py` | 相机和 STM32 都在派上，需要一个"图传 + 串口桥"的常驻进程 |
| 串口可走 TCP | `host/serial_link.py` | `open_best_link` 认 `tcp://IP:5058`，上位机其它代码零改动 |
| 相机可走 TCP | `vision/remote_camera.py` + `camera_factory.py` | `Stream` 只认 `cam.open()/cap.grab()/cap.retrieve()`，用同名接口即可无缝替换 |
| 视觉页只改 1 行 | `vision/gui.py`（`Stream.__init__`） | 1240 行主界面不动，降低回归风险 |
| 新入口 | `main.py` | 支持 `--pi HOST`；默认行为与 final 完全一致 |
| 文档/组网/自测 | `net/`、`docs/`、`tests/` | 现场没人给你讲怎么连 WiFi，得写下来 |
| **一键连派** | `host/pi_link.py` + `host_gui.py` + `main.py` | 端口下拉框直接列出「派 …」，选中点连接 = 控制走 TCP + 视觉自动切远程并开双画面，不用记 IP、不用命令行参数 |
| **接近段示教** | `host/approach_route.py` + `host_gui.py`（四轴页） | X/Y/Z 从「整机上电初始位 → 抓取位」示教一次、存 `runtime/approach_route.json`，跑序列/抓取前自动重放（开环相对位移，转速可改） |
| **回上电初始位** | `host/journey.py` + `host_gui.py`（四轴页） | 从上电起记录四轴（含托盘）全部移动，跑完一轮点一次「原路返回初始位」→ 倒序 + 方向取反重放，返回成功清空记录 |

**没有改的东西**：STM32 固件（协议零改动）、上位机协议、视觉算法、SAM 模型、标定算法。

---

## 5. 安全设计（三层，逐层兜底）

1. **GUI 层**：右上角「停止全部 (Esc)」→ 立即发四轴停车帧。
2. **派端看门狗**：`pi_agent.py` 若 1.5 s 没收到笔记本任何字节 → 主动向 STM32 写四轴停车帧（`01/03/04/05 FE 98 00 6B`）。
3. **固件层**：原有 3 s 心跳失联停车（`readback_mask` / control 心跳）。

所以：**拔网线、关笔记本、WiFi 掉线，机械臂都会在 1.5 s 内自己停**。

---

## 5.1 夹爪三档角度与任务流程（180 / 150 / 135）

夹爪是**位置舵机**，用角度表达三档，全部可在「参数」页改：

| 角度 | 参数 | 含义 | 什么时候用 |
|---|---|---|---|
| 180° | `gripper_open_deg` | 完全张开 | 上电默认位、放料松开 |
| 150° | `gripper_ready_deg` | 预备位 | **连上上位机后自动打到**；每件放完也自动回这里 |
| 135° | `gripper_grab_deg` | 夹紧 | 抓取瞬间 |

标准一件的流程：

1. 上电 → 夹爪 180°（完全张开）。
2. 笔记本连上（`hello` 变在线）→ 上位机**自动补发一次** `gripper ready`，夹爪到 150° 预备位。
   （自动发送失败只写日志、不弹框，可手动点「舵机夹爪 → 预备(150)」补救。）
3. 抓取：`gripper grab` → 135° 夹紧。
4. 放到盒里：`gripper open` → 180° 松开 → **先抬 Z 到安全高度** → 等 500 ms → `gripper ready` → 150° 回预备位 → 下一件。
   （先抬再收口：180°→150° 如果在盒子里收，容易蹭到物块或盒壁。）

视觉自动生成的抓取序列已经按上面顺序生成：开头一次 `ready`，每件 `grab` → … → `open` → 抬 Z → `wait_ms 500` → `ready`。
`wait_ms 500` 可在 `vision_grasp.json` 的 `gripper.ready_settle_ms` 改（默认 500）。

> ⚠ 这一版**必须重烧固件**才生效：新动作 `gripper ready`、两个新参数都是固件里的。
> Keil 打开 `ghr/10.15/Project/STM32F407.uvprojx` 按 F8 下载（或烧 `ghr/10.15/Project/Objects/STM32F429.hex`）。
> 旧固件收到 `ready` 会回 `E02 unknown_param` / `E01`，夹爪不会动。

另有调试兜底参数 `gripper_hold_max_ms`（默认 0 = 不卸力，范围 0..60000，固件参数表是 uint16）：

- 0：一直保持当前角度（正常比赛用）。
- 非 0：非夹紧状态最多保持这么久就卸力，防止夹爪堵转发热（调试用，上位机只暴露默认值和中文字段说明）。

---

## 5.2 接近段示教（X/Y/Z：整机上电起点 → 抓取位）

**要解决的问题**：视觉只负责「托盘平面内的 X/Y + 抓取时序」，但机械臂从**整机上电的初始位**到「能看见托盘中心」的这段路（Z 高度、X/Y 大行程）以前每次都得手点一遍。

**做法**：把这段路**示教一次、存成文件、以后自动重放**。

- 参与轴：**X(4) / Y(5) / Z(1)**；**托盘(3) 不参与**（托盘是转位机构，不是接近路径）。
- 起点：**整机上电后的位置**。示教时不要做位置清零——重放就是在上电初始位的基础上按顺序做相对位移（顶栏的「一键回0/开机回基准」已按需求删除，界面不再有这两个按钮）。
- 文件：`10.21/runtime/approach_route.json`（`{version, saved_at, steps:[{axis,dir,rpm,acc,pulses}...]}`，最多 200 段，单段 ≤ 100 圈）。

**怎么示教（四轴控制页底部「接近段示教」）**：

1. 连接控制板（串口或派都行）。
2. 点「开始示教」——按钮变红「结束并保存 ●」，之后**每一次 X/Y/Z 点动都会被按顺序记成一段**（托盘点动不记）。三种点动都支持：『相对走』立即记一段；『绝对走到此』按软位置换算成一段；『常用走』要等停止/急停才结算成一段。每记一段日志会打印「已记录第 N 段：X +3200 脉冲」。
3. 用四个轴面板的正常点动按钮（相对走、常用走、绝对走到此），从上电初始位一步步挪到抓取位。点错了就「撤销上一段」。
4. 点「结束并保存」→ 落盘，状态行显示 `N 段｜X +… Y … Z …｜已保存 <时间>`。
5. 点「执行接近段」可以**立刻重放一遍**验证（确认路径上没人没物；开环，没有回读保护）。

**自动执行**：勾选「运行序列/抓取前自动执行接近段」（默认勾上）后，点「动作序列 → 运行」时先走完接近段，再启动序列。视觉页生成序列后点运行，同样先走接近段。

**速度后期调整**：「重放转速（0=沿用示教）」填一个 RPM 就统一改写所有段的转速（填 0 用示教时每段的转速）；也可以示教时就用最终速度。每段等待时间按 `脉冲 / (转速×3200/60)` 估算 + 400 ms 缓冲，**没有到位回读，所以是开环等待**。

**局限（诚实说明）**：

- 开环重复：没有编码器回读（PA3 回读线未接），脉冲数与实际位移的误差会累积；每次上电的初始位必须一致（别手推机械臂、别中途掉电）。
- 「停止全部 (Esc)」会立刻清空重放队列并停车；重连后固件会锁存停止，上位机自动补发一次 `stop_clear`（只解锁，不产生运动）。
- 换了相机位置、动了托盘、或改了机械限位 → 接近段要重新示教（它只描述 X/Y/Z 的相对路径，不含托盘角度）。

---

## 5.3 回上电初始位（自动记录全部移动 + 一键原路返回）

**要解决的问题**：跑完一轮之后机械臂停在托盘上方，下一轮要从「整机上电的位置」重新开始；手点回去既慢又容易撞。这段路每次都不一样（取决于上一轮走到哪），所以**不能像接近段那样预先示教**——只能边跑边记，再原路倒回去。

**做法**：从连上控制板开始，**四轴（Z/托盘/X/Y）的每一次移动都记进一条流水账**（含托盘转位），点「原路返回初始位」就按记录**倒序 + 方向取反**重放一遍。

- 记录文件：`10.21/runtime/journey.json`（`{version, boot, started_at, steps:[{axis,dir,rpm,acc,pulses}...]}`，最多 2000 段，超出丢最旧的）。
- 记录时机：每一段运动命令**一发出就记账**（不等到位），所以「常用走（速度模式）」那段是**按时间×转速估算**的脉冲数（会有误差）；相对走/绝对走到此是精确脉冲数。
- 固件重启（启动号变化）→ 自动清空记录并提示「检测到固件启动号 X：移动记录已清零」，因为上电位置已经变了，旧记录倒回去就是错的。
- 返回成功后**记录会清空**（避免连点两次往反方向走）。

**怎么用（四轴控制页底部「回上电初始位」）**：

1. 上电、连接控制板——从这一刻开始记账。
2. 正常操作/跑序列（接近段、视觉抓取都行），状态行会显示 `N 段｜净 Z … X … Y …`（净位移是各轴正向脉冲减反向脉冲）。
3. 一轮结束后点「原路返回初始位」→ 弹窗确认段数与预计秒数 → 自动倒序重放。
4. 到位后目视确认（开环，没有回读），再开始下一轮；点「清空记录」可手动清零。

**局限（诚实说明）**：

- 开环无回读：返回的落点误差 = 各段估算误差之和，跑很多轮后可能越差越多 → 建议每 2–3 轮重新上电（或在关键位置用「位置清零」+ 重新示教接近段）校准一次。
- 中途手推机械臂/掉电/急停后手移过 → 记录里的「物理位移」就不再成立，应上电重来。
- 「停止全部 (Esc)」会立即中断返回并清空返回队列（记录保留）。

---

## 6. 已知限制（诚实清单）

- 派端相机分辨率默认 1280×720、JPEG 质量 80：标定需要重做（原点/四点/像素当量都变了）。**换相机或换分辨率必须重新标定**。
- 图传是"按需拉帧"，不是视频流：视觉页在实时分割模式下帧率取决于网络 + 识别耗时（本机 SAM 约 1 fps）。
- SAM-B 在派上不可用（20–30 s/帧）；若以后要全派化，换 YOLO11n-seg/RKNN（`docs` 里有路线提示）。
- **WiFi 带宽**：两路 1920×1080 JPEG(q80) @30 fps 约需 4–7 MB/s，树莓派的 2.4G/普通 5G 上行扛不住；预览建议派端 1280×720（`pi/config.json` 里改），或在视觉页把分辨率调小。真机实测掉帧就先降分辨率/帧率。
- 远程相机不支持 `CAP_PROP_*` 调参：增益/曝光要在派上用 `v4l2-ctl` 固定，否则两路相机自动曝光会互相打架。
- **相机编号要映射**：`vision/config.json` 的 `top_camera_index/side_camera_index` 是"本机设备号"（Windows 上常是 1、2），而派端 `pi_agent.py` 的槽位永远是 `--cam-index` 的**出现顺序**（0、1）。用 `remote.cam_map`（或环境变量 `ROBOT_PI_CAM_MAP`）把两个编号对上，**USB 插拔顺序一变，映射就变**：
  - 本包装配（2026-10-23 实测）：派槽 0 = 斜上位（侧视），派槽 1 = 正俯视（顶视）→ `"cam_map": {"1": 1, "2": 0}`（已写进 `vision/config.json`）。
  - 插反的情况：`ROBOT_PI_CAM_MAP=1:0,2:1`。
  - 判断哪一路是顶视：`python net\find_pi.py <派IP> --full --cam 0` 与 `--cam 1` 各取一帧看画面，**正俯视托盘**的那一路就是顶视；对不上时视觉页会报"相机 N 未配置（可选：0）"。
- 本包**不含** STM32 固件源码（在仓库 `ghr/10.15/`）；比赛前请确认固件已烧录且 USB CDC 能枚举。

---

## 7. 自测与验收记录（本机实测，2026-10-21）

这些测试**不需要真机**（派端用 `--fake-camera`/`--fake-serial`），比赛前换机器后先跑一遍，绿了再上硬件。

**一条命令跑完全部**：

```bat
selftest.bat            :: 全部 7 步，约 2 分钟，最后打印 ALL CHECKS PASSED
selftest.bat /quick     :: 只跑无界面的 1~6 步（CI / 远程 shell）
```

`selftest.bat` 会依次跑：上位机 164 个用例 → 视觉导入+权重检查 → 起假派 → 派端 24 项自测 →
派↔真上位机协议 34 项 → 笔记本 TCP 端到端 5 项 → GUI 冒烟 + 8 页界面；结束时自动杀假派进程。
本机（2026-10-21）实测：**7/7 步全绿，RESULT: ALL CHECKS PASSED**。

| 测试 | 命令 | 覆盖 | 本机结果 |
|---|---|---|---|
| 上位机全量离线用例 | `cd host && python -m unittest discover -s tests -t tests` | 协议/会话/序列/标定/视觉导入/界面冒烟 | **164 tests OK** |
| 端到端（假派） | `python tests\test_net_local.py` | 远程取帧、SocketLink+握手+四轴查询、不可达不崩、ER 包处理、相机编号映射 | **5/5 PASS** |
| 视觉页 GUI 冒烟 | `python tests\gui_remote_smoke.py` | 真窗口 + 远程相机出图（截图 `runtime/remote_gui_smoke.png`） | **顶视 495 帧 1920×1080** |
| 派端自测 | `python pi\tests\selftest_client.py`（先起 pi_agent） | 图传 4 类 + 串口桥 7 类 | **24~27 PASS** |
| 派端↔真上位机协议 | `python pi\tests\test_host_protocol.py`（先起 pi_agent） | 19 条命令、E01/E10/E12、去重、并发连接 | **34/34 PASS** |
| 8 页界面 | `python main.py --selftest` | 建界面、切 8 页、截图到 `runtime/selftest_*.png` | **8/8 页** |
| SAM 权重可用 | 见 `vision/auto_segment.py` | 加载 `models/sam_b.pt` 并分割 `docs/crop_test.jpg` | **加载 2.8 s / 分割 2.3 s / 5 个目标** |

两个端到端截图（可直接看）：

- `runtime/remote_gui_smoke.png`：视觉页显示的是**派端假相机的远程画面**（右上角有 FAKE CAM 时间戳、顶视 80 fps）。
- `runtime/wifi_control_connected.png`：串口框是 `tcp://127.0.0.1:15058`，状态「在线」、固件 `pi-sim-0.1`、最后通信 0.3 s 前——证明**上位机协议原封不动跑在 TCP 上**。

---

## 7.1 真机联调记录（2026-10-22，Orange Pi 4 Pro 12G + 双 USB 相机 + STM32）

环境：Orange Pi 4 Pro（A733，Ubuntu，python3 3.14.4 + cv2 4.10 + pyserial 3.5），
`python3 pi_agent.py --cam-index 0 --cam-index 2`，笔记本走 WiFi（172.20.240.14）连派。

| 项目 | 实测结果 |
|---|---|
| 图传建连 + 取帧 | 连接 51 ms；1280×720 / 83 KB / 311 ms |
| 串口桥建连 | TCP 31 ms（派端 `/dev/ttyACM0` 115200 8N1，STM32 CDC 已枚举） |
| `hello` 握手 | 1.61 s → `fw=0.1.0 boot=0000009C axes=1/3/4/5 readback=1/3/4/5` |
| `status` | `latch=1,owner=pc,running=-,queued=0,gripper=180` |
| `param_get` | `speed_rpm=60`、`readback_mask=15`（`accel` 不在白名单 → E02 unknown_param） |
| `motor_query` vel/pos/flag | 12 次里 11 次 `E04 查询超时`（PA3 回读线实际未接；与历史结论一致） |
| 视觉页真画面 | 顶视 8.4 fps / 侧视 6.6 fps（1280×720 q80）；截图 `runtime/real_pi_live.png`、`real_cam0.jpg`、`real_cam1.jpg` |
| 相机槽位 | 派上 `--cam-index 0 --cam-index 2` → 槽位 0/1；笔记本端 `ROBOT_PI_CAM_MAP=1:0,2:1` 映射后顶视/侧视各就各位 |

**这次真机验证的结论**：`笔记本 GUI ⇄ WiFi ⇄ 派(图传+串口桥) ⇄ STM32` 整条链路通，
命令通道（hello/status/param_get）与图像通道都能用；**唯一没通的是四轴编码器回读**
（驱动器 TX → 派/笔记本的物理回读线没接），所以位置只能靠"软位置"记账。

---

## 7.2 真机联调记录（2026-10-23，换机 + 新 WiFi）

环境：另一台 Orange Pi 4 Pro（同型号，**重新装过系统**：Ubuntu 26.04 "Resolute"、python3 3.14.4，
需要 `sudo apt-get install -y python3-opencv python3-serial` 才有 cv2/pyserial），
WiFi `MIFI-2419`：派 `192.168.100.198`、笔记本 `192.168.100.199`；
`python3 pi_agent.py --cam-index 0 --cam-index 2`（真机模式，非 fake）。

| 项目 | 实测结果 |
|---|---|
| 图传建连 + 取帧 | 连接 70 ms；1280×720 / 69 KB / 376 ms |
| 串口桥建连 | TCP 5 ms；派端 `/dev/ttyACM0` 115200 8N1，STM32 CDC 已枚举 |
| `hello` 握手 | 1.61 s → `fw=0.1.0 boot=00000001 axes=1/3/4/5 readback=1/3/4/5` |
| `status` | `latch=0,owner=pc,running=-,queued=0,gripper=180` |
| `param_get` | `speed_rpm=60`、`readback_mask=15`（`accel` → E02 unknown_param） |
| `motor_query` vel/pos/flag | 12/12 `E04 查询超时`（PA3 回读线未接，与 10-22 一致） |
| 视觉页真画面 | 顶视 3.8 fps / 侧视 5.4 fps（1280×720 q80）；截图 `runtime/real_pi_live.png`、`real_new0.jpg`、`real_new1.jpg` |
| 动作通道 | `stop_clear` / `gripper open` / `gripper grab`(135°) / `gripper open` 全部 ok，两张截图里爪子确实开合（`real_new_gripper_open.jpg` / `real_new_gripper_grab.jpg`） |
| 相机槽位 | 换机后插拔顺序变了：**槽 0 = 侧视、槽 1 = 顶视** → `cam_map {"1": 1, "2": 0}`（已写进 `vision/config.json`；当晚再次插拔后槽位又反过来，见 §7.3） |
| 相机总线 | 实测一个相机在 `xhci`(USB3)、另一个经 hub 在 `sunxi-ehci`(USB2) → 走 USB2 的那路帧率明显低（3.8 vs 5.4 fps）；**两路都插 USB3 口**能拉平 |

**这次验证的结论**：换一台派、换一套网络后，只要改三处 —— `cam_map`、WiFi/IP、重装依赖
（`python3-opencv`/`python3-serial`）——整条 `笔记本 GUI ⇄ WiFi ⇄ 派(图传+串口桥) ⇄ STM32`
链路就又能跑通，**协议层与固件零改动**。

⚠ 相机是重新插过的，`vision/config.json` 里的四点（`top_points`/`side_points`）与 X/Y 标定
是上一套装配的旧值，**必须在视觉页重新框四点、再重做 X/Y 标定**（见 `docs/换分辨率与重标定清单.md`），否则抓不准。

---

## 7.3 真机联调记录（2026-10-23 夜，双相机都挂在同一个 USB2 hub）

环境：同一台 Orange Pi 4 Pro（`192.168.100.198`），笔记本 `192.168.100.199`，WiFi `MIFI-2419`；
两台相机 + STM32 全部插在派上；`pi_agent.py` 真机模式（`--cam-index 0 --cam-index 2`）。

| 项目 | 实测结果 |
|---|---|
| 派端 USB | `lsusb` 见 `1e45:8022`×2 与 `0483:5740`，**两台相机都挂在 Bus003 的同一个 USB2 hub 上** |
| 采集节点 | `cv2 CAP_V4L2`：`/dev/video0`、`/dev/video2` 可读（1280×720），video1/video3 打不开 → 索引仍是 `0`/`2` |
| 图传 | 建连 48 ms；取帧 1280×720 / 75 KB / 189 ms；GUI 里 **顶视 11.0 fps、侧视 11.8 fps** |
| 串口桥 | 建连 38 ms；`hello` → `fw=0.1.0 boot=0000000x axes=1/3/4/5` |
| 新固件确认 | `param_get gripper_ready_deg=150`、`gripper_hold_max_ms=0` → **2026-10-23 编译的固件已烧进板子** |
| 新动作确认 | `gripper ready` → `action=ready, angle=150, settle_ms=200`（夹爪真的转到 150°） |
| 相机槽位 | 这次插拔顺序又变了：**槽 0 = 顶视、槽 1 = 侧视** → `cam_map {"1": 0, "2": 1}`（已写进 `vision/config.json`） |
| 侧视取景 | 侧视相机被丝杠/机械臂挡住上半屏 → **需要调整安装角度**，否则侧视没法做融合校验 |
| 一键连派 | 双击 `run.bat` → 端口框自动出现「派 robot-pi 192.168.100.198」→ 点连接 → 控制在线（`fw=0.1.0`）+ **视觉自动开双画面**（顶视 6.4 / 侧视 4.9 fps）；截图 `runtime/runbat_pi_entry.png`、`runtime/pi_oneclick.png`；host 单测 **164 OK** |

**这次踩到并已修掉的坑**：两台相机都挂在 **USB2 hub** 上时，`pi_agent.py` 原来只设宽高不设像素格式，
V4L2 默认走 YUYV（720p 单路 ≈ 55 MB/s），两路一起开直接带宽不够，日志里表现为
"相机 0 连续抓帧失败，重开设备" 无限循环。修法：`_open_capture()` 在 Linux 上先设
`CAP_PROP_FOURCC = MJPG`（压缩后约 1/10 带宽）。**以后只要看到"连续抓帧失败"，先想像素格式/带宽。**

---

## 8. 文档索引（都在 `10.21/` 内）

## 8. 文档索引（都在 `10.21/` 内）

| 想知道什么 | 看哪里 |
|---|---|
| **赛前/现场照着做** | `docs/比赛现场操作卡.md`（开机顺序、验通、标定、故障速查、红线） |
| **改分辨率/换相机后要重做哪些标定** | `docs/换分辨率与重标定清单.md` |
| 两端到底发什么字节 | `docs/网络协议-接口约定.md` |
| 笔记本和派怎么组网（无互联网） | `net/组网指南.md` |
| 派怎么装系统/装依赖/开机自启 | `pi/README.md` |
| 上位机每个键干什么 | `host/README.md` |
| 一体化软件完整使用说明（8 页逐条） | `docs/一体化使用说明.md` |
| 视觉怎么标定托盘 | `docs/视觉-标定操作手册.md` |
| 视觉和上位机怎么联调（历史方案） | `docs/视觉-联调与通讯方案.md`、`docs/视觉-无线组网联调.md` |
| 固件引脚/协议/历史坑 | `docs/交接文档-20261016.md`（最新）+ `docs/EMM_V5_串口协议与工程指令总结.md` |
| 换电脑怎么复刻环境 | `docs/环境复刻流程.md` |
| 赛题硬约束（必读） | `docs/newpdftext.txt` |

---

## 9. 五分钟故障排查

| 现象 | 先查这个 |
|---|---|
| `run.bat` 一闪就退 | 用 `cmd` 手动跑看报错；确认 `.venv` 或 `Qwen\.venv` 存在 |
| 视觉页两路都是黑的 | 本地模式查相机编号；WiFi 模式查 `net/find_pi.py --full` |
| 连不上 `tcp://...` | Windows 防火墙（跑 `net/firewall-allow.ps1`）、派端 `pi_agent` 是否在跑 |
| 端口下拉框里没有「派 …」 | UDP 5056 广播被拦/AP 隔离：直接手填 `tcp://<派IP>:5058` 再点连接（效果一样）；或先跑 `net/find_pi.py` 拿 IP；`runtime/pi_last.json` 里存着上次连过的 IP |
| 连上就断 | 派 WiFi 省电（`sudo iw dev wlan0 set power_save off`）、信号弱 |
| 机械臂动一下就停 | 派端看门狗（1.5 s 没数据）在保护；检查笔记本到派的链路 |
| 派上相机打不开 | `v4l2-ctl --list-devices`；USB 相机别都插在同一个 USB 2.0 hub 上 |
| 派端日志刷"相机 N 连续抓帧失败，重开设备" | 两路相机挤在同一个 USB2 hub、又用 YUYV 时带宽不够。`pi_agent.py` 已改成 Linux 上优先 MJPG；仍失败就把相机拆到不同 USB 控制器（`lsusb -t` 看 Bus）或降到 640×480 |
| 重连后一点运动就回 `E10`（需要先清除停止锁存） | 固件在 3 s 收不到上位机字节时会锁存停止；GUI 自 2026-10-23 起在 hello 连上后**自动发一次 `stop_clear`** 并写日志。若仍被拒，手动点右上「清除停止锁存」；也可查 `status` 的 `latch=` |
| 命令回包奇怪（截断/交错、`boot=` 一直涨、`rx` 与请求对不上） | **派上有别的程序也在开 `/dev/ttyACM0`**。查 `P: pgrep -af python3` 和 `P: systemctl --user list-units --all \| grep -Ei 'vision\|stm32'`；本机实测过 `vision-stm32-bridge.service`（旧版 photo_test 遗留）在循环发 hello，已 `systemctl --user disable --now` 停用；确认只有一个进程持有串口：`P: fuser -v /dev/ttyACM0` |
| 派上 `import cv2` / `import serial` 报 `ModuleNotFoundError`（刚重装过系统/换了卡） | `echo orangepi \| sudo -S apt-get install -y python3-opencv python3-serial`；再把 `pi/` 和 `common/` 重传到 `~/robot1021/`（先 `mkdir -p ~/robot1021`） |
| plink/scp 报 `Host key not in manually configured list` | 重装系统后 SSH 主机键会变（本机实测旧 `WIMfsjTCe+…` → 新 `SHnztqS6…`）；用新指纹传 `-hostkey SHA256:...`，或先手动 `plink -pw <密码> orangepi@<IP> true` 确认 |
| 视觉页顶视/侧视画面互换了 | 相机 `cam_map` 与实际插口对不上：按上面 §6 的"相机编号要映射"改 `vision/config.json` 的 `remote.cam_map` |
| **点了「连接」没反应**（状态栏一直「未连接」/现在会弹「连接失败」） | 九成是**派重启后 pi_agent 没跑**（重启不会自动起它，除非装了自启）。`P: systemctl is-active robotpi`；没装就 `P: bash ~/robot1021/pi/install.sh`（会按当前机器生成 unit 并 `enable --now`），或临时 `P: ~/robot1021/start_agent.sh`；然后 `P: ss -ltn \| grep 5058` 确认在听。GUI 自 2026-10-23 起连接失败会**弹窗+状态栏红字**并给排查建议，不再只写日志 |
| 派开机后 agent 没自启 | `10.21/pi/install.sh` 自 2026-10-23 起**现场生成** unit（用户/目录/`--cam-index` 自动填），不再原样拷 `robotpi.service`（那份是树莓派示例，用户 `pi`、目录 `/home/pi/10.21/pi`，直接拷会启动失败且只在 journal 里报错） |
| 示教完点运行，机械臂没先走接近段 | ① 四轴页勾选框「运行序列/抓取前自动执行接近段」是否勾上（默认勾）；② 状态行是否显示 `N 段`（`0 段` = 没保存成功，或 `runtime/approach_route.json` 被删）；③ 「原始日志」里应有 `开始执行接近段` → `接近段执行完毕`；④ 重放中途断链会主动中断并写 `接近段中断：链路断了` |
| 接近段每次落点不一样 / 明显走偏 | 开环无回读，误差会累积。确认 ① 每次都是**冷启动/上电后的同一个初始位**（别手推机械臂、别中途掉电）；② 示教时的点动都发生在「开始示教」之后（之前挪的不算）；③ 改过机械限位/换了相机支架/动过托盘 → 重新示教。要根治只能接上 PA3 回读线（四轴编码器回读，目前 `motor_query` 全 `E04`） |
| 「原路返回初始位」点不动 / 提示没记录 | ① 记录是**从连上控制板开始**记的：连上之前用手推的位移不算；② 状态行 `0 段（上电后还没动过）` = 真没动过，或固件重启后自动清零了（「原始日志」里会有 `检测到固件启动号 …：移动记录已清零`）；③ 没连控制板时按钮会提示先连接 |
| 返回后停的位置和上电位置差一截 | 开环无回读：**「常用走（速度模式）」那段是按时间×转速估算**的，误差最大；跑多轮会累积。补救：少用常用走示教、每 2–3 轮重新上电校准一次、关键段改用「相对走」精确脉冲 |
| 连点两次「原路返回初始位」把机械臂又送回托盘上方 | 设计如此：**返回成功后记录会清空**（防止连点往反方向走）。要再来一轮，先正常抓取/移动产生新记录 |