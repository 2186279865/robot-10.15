# 01-交付包 —— 机器人视觉分拣系统（可直接用的一版）

> 这个文件夹是**整套系统**：STM32 固件 + 香橙派（Orangepi / Raspberry Pi）端 + 笔记本端视觉与上位机。
> 它自己带模型权重、协议实现、部署脚本和文档，**不需要**本仓库其它任何目录。
> 想改代码、看历史、查资料 → 去 `../02-资料库/`；想试新想法 → `../03-实验台/`。

一句话数据链：

```
两个 USB 相机 ──► 香橙派（采集 + JPEG，TCP 5057）──┐
                                                   ├──► 笔记本（视觉识别 + 上位机 GUI）
STM32 四轴 ◄── /dev/ttyACM0 ◄── 香橙派（串口桥，TCP 5058）─┘
```

- 相机和 STM32 都插在**派**上；派只负责"取图 + 转发串口字节"，很轻。
- 识别（SAM 分割 + 融合 + 分类）和界面在**笔记本**上跑，吃笔记本的 GPU/CPU。
- 两边在**同一个 WiFi/热点**里，用四条 TCP/UDP 通道通信（5056 发现、5057 图传、**5058 串口桥控制口（独占）**、5054 串口桥观察口（只读））。
- **控制口同一时刻只允许一台上位机**：另一台再连会被拒绝并在日志写明；只想看回包用只读观察口 5054。

---

## 1. 三大块分别是什么

| 目录 | 是什么 | 跑在哪 | 一句话 |
|---|---|---|---|
| `stm32/` | 四轴抓取固件（Keil 工程 + 源码 + 现成 hex） | STM32F407 | 收上位机命令 → 发 EMM V5 帧控 4 台步进；PWM 控舵机夹爪；USB CDC 当虚拟串口 |
| `orangepi/` | 派端服务 `pi_agent.py` + 部署脚本 | 香橙派 / 树莓派 | 相机取帧（JPEG 按需给）、串口透传、UDP 广播自己 |
| `host/` | 上位机（8 页 tkinter GUI：四轴/夹爪/参数/序列/视觉目标/日志…） | 笔记本 | 人机界面 + 动作序列 + 标定 + 安全联锁 |
| `vision/` | 视觉算法（SAM 分割、双摄融合、规则分类）+ `sam_b.pt` 权重 | 笔记本 | 输出托盘坐标 (u,v) 给上位机 |
| `common/` | 两端共用的线路协议 `wire.py`（只有标准库） | 笔记本 + 派 | 图传/串口桥/发现 的报文格式定义 |
| `net/` | 组网与排查工具（`find_pi.py`、`firewall-allow.ps1`、组网指南） | 笔记本 | 找不到派、防火墙不通时用 |
| `docs/` | 使用说明、协议约定、现场操作卡、换分辨率重标定清单 | — | 人看的 |
| `runtime/` | 运行期状态与输出（示教记录、移动记录、标定、截图、日志） | 笔记本 | **可以整目录清空**，软件会自己重建 |
| `tests/` | 跨端联调测试（TCP 图传/串口桥/发现/cam_map/ER 包） | 笔记本 | 改完代码跑一下 |

---

## 2. 三步跑起来（笔记本端）

> 首次：`setup.bat` 建本地 `.venv` 并装依赖（numpy/opencv/torch/ultralytics/pyserial，约 1–2 GB，用清华镜像）。
> 之后：只要双击 `run.bat`。
>
> 说明：`.venv` 是**本机产物**，不进 git、不随包拷贝。本机（开发机）里的 `.venv` 是指向 `../03-实验台/Qwen-VLM/.venv`
> 的目录联接（junction），所以现在直接就能跑；**换一台电脑必须自己跑一次 `setup.bat`**（离线环境见 §9）。

```bat
setup.bat            :: 只需一次（或换机器时）
run.bat              :: 本地模式：USB 相机 + USB 串口直接插笔记本
run.bat --pi 192.168.100.198   :: WiFi 模式：相机和 STM32 在派上
run.bat --sim        :: 无硬件，模拟后端，纯看界面
```

**WiFi 模式不用记 IP**：`run.bat` 打开后，端口下拉框里会自动出现

```
派 robot-pi 192.168.100.198（图传 5057 / 串口 5058）
```

选中它 → 点 **连接** → 右上角变 **在线**、右边显示固件版本号 → 视觉页点 **打开双画面** 就能看到两台相机的实时画面。

连上后判断"真的通了"的四个标志：
1. 右上角 **在线**，旁边 `固件 0.1.0 启动 XXXX`；
2. 右下角 **已连接（软位置）**；
3. 视觉页两路画面都有图（>5 fps 算正常，同 USB2 hub 时会掉到 5–10 fps）；
4. 点「四轴控制」页某个轴 **使能** → 日志出现 `ok`。

---

## 3. 派端怎么装（一次性）

把 `orangepi/` 整个目录拷到派上（U 盘 / `scp` / `pscp` 都行），然后：

```bash
cd orangepi
chmod +x install.sh start.sh
./install.sh          # 装 apt 依赖 + 生成 systemd 服务（自动用当前用户名/路径）
```

`install.sh` 会做：装 `python3-opencv python3-serial`（没网就跳过）→ 生成 `/etc/systemd/system/robotpi.service`（`ExecStart=/usr/bin/python3 <当前路径>/pi/pi_agent.py --cam-index 0 --cam-index 2`）→ 开机自启。

常用命令：

```bash
systemctl status robotpi          # 看服务状态
journalctl -u robotpi -f          # 跟日志
sudo systemctl restart robotpi    # 改配置后重启
```

手动前台跑（调试用）：

```bash
cd orangepi/pi
python3 pi_agent.py --cam-index 0 --cam-index 2      # 真相机
python3 pi_agent.py --fake-camera --fake-serial      # 无硬件，假图假电机
```

---

## 4. 固件怎么烧

现成文件（**2026-10-25 21:43 这一版，含夹爪三档 + 接近段示教 + 闭环 `motor_goto`**）：

```
stm32/hex/STM32F429.hex
```

- 用 STM32CubeProgrammer / ST-LINK Utility 直接烧这个 hex；或
- 用 Keil 打开 `stm32/firmware/Project/STM32F407.uvprojx` 按 F8。

> 文件名里的 `F429` 是历史遗留，芯片其实是 **STM32F407**，不影响烧录。
> 别烧 `stm32/hex/STM32F429_diag.hex`（那是 10 月 16 日的诊断固件）。

烧完的验证（两条，都要过）：
1. 上位机「舵机夹爪」页点 **预备(150)** → 日志回 `action=ready, angle=150`，说明夹爪三档这版在跑；
2. 命令行 `\.venv\Scripts\python.exe tools\goto_check.py --port COM6 --dry-run` → 能读到 `counts_per_rev / goto_tol_counts / goto_rpm` 三个参数，说明闭环这版在跑（旧固件会显示 `None` 或回 `E02`）。

> ⚠ **老固件没有 `motor_goto`**：四轴页勾「闭环」再点「相对走」会回 `E01/E02`。新固件 hex 大小是 **89,098 字节**（上一版 81,951）。

编译细节见 `stm32/docs/烧录与编译.md`。

---

## 5. 第一次用要标定

视觉要"看得准"，必须先标定（**换相机、换分辨率、动过相机安装位置 → 全部重做**）：

1. **四点透视**（顶视）：视觉页点「顶视四点框选」，按 左上→右上→右下→左下 点托盘四角；
2. **空背景**：托盘清空，点「采集空托盘」；
3. **X/Y 标定**：切到「X/Y 标定」页，用三种模式之一（marks / push / step）把"图像坐标 ↔ 实际脉冲"对上；
4. **Z 与抓取高度**：在「视觉目标」页示教 Z 高度与抓取偏移（这部分是人工示教值，不自动算）。

详细步骤：`docs/一体化使用说明.md`、`docs/视觉-标定操作手册.md`。

---

## 5.4 自动抓放状态机（2026-10-26 新增，骨架）

`host/auto_runner.py`——把"拍照 → 规划 → 走通道 → 对准 → 抓 → 抬 → 回 → 放 → 显示"编成状态机，
每一步都等回包（不是 sleep），失败一律进 `FAULT`：

```
IDLE → CAPTURE → PLAN → CORRIDOR_OUT → ALIGN → DESCEND → GRASP
     → LIFT → RETREAT → CORRIDOR_BACK → RELEASE → DISPLAY → (还有货 → CAPTURE / 没货 → DONE)
```

- 通道固定"先 Y 让开、再抬 Z"（初始位上方有遮挡，机构不能改），回程严格反序取反；
- **类别 → 盒子动态分配**：`host/box_map.py`，第一件见到某类就占一个空盒（最多 6 类 6 盒），
  手动 `vision_grasp.json` 的 `tray.class_to_box` 优先；落在 `runtime/box_map.json`；
- 软限位：`host/limits.json`（`{"4": {"min": -50000, "max": 42000}, ...}`），越界直接 `E15` 进 FAULT；
- 自测：`cd host && ..\.venv\Scripts\python.exe -m unittest discover -s tests -t tests`
  （182 个用例，其中 `test_auto_runner.py` 8 个走假后端，不碰硬件）；
- 试跑：`python host\auto_runner.py --dry-run` 打印通道/限位配置并用假后端跑一遍。

## 5.5 派端显示页（赛题强制要求）

`orangepi/pi/pi_display.py`——派接 HDMI 小屏（我们现在是 7 寸 1024×600），全屏大字显示
"第几件 / 什么 / 进哪个盒"。上位机用 `host/display_link.py` 往 `tcp://派:5059` 推 JSON 行：

```json
{"state":"DISPLAY","item":3,"total":12,"name":"五棱柱|蓝","box":2,"placed":3,
 "message":"第 3 件：五棱柱·蓝 → 2 号盒"}
```

派上启动：`python3 pi_display.py`（`--windowed` 调试、`--fake` 自演示）。屏保/旋转已设好：
`~/.local/bin/screen-setup.sh` + `~/.config/autostart/screen-setup.desktop`（不黑屏、旋转 180°）。

## 5.6 一键走出固定路线（标定前用）

初始位上方有障碍，X 不能动。所以第一次标定（以及以后每次重新标定）之前，先从初始位走固定通道到工作位：

1. 「X/Y 标定」页最左边点 **一键走出固定路线**
2. 确认弹窗后机器自己走：先 `Y -13144 脉冲`（= -269,194 counts，出通道），再 `Z -152000 脉冲`（抬起，约 47.5 圈 = 最上）
3. 日志出现「固定路线完成」后，再用点动把爪子对到标注点上方，开始 4 点标定

- 两步都用固件闭环 `motor_goto`：走完并校验过才算 ok；失败写日志（E14/E10 等），先点「停止所有轴」再重试
- 参数在 `runtime/prep_pose.json`（`y_pulses / y_rpm / z_pulses / z_rpm`），改完保存即生效，不用重启
- 只动 Y、Z，不动 X、不动托盘；固件处于停止锁存时会先自动 `stop_clear`

## 5.7 单位约定（重要，只看这一段就够）

**对外一律用「脉冲」。** 1 圈 = **3200 脉冲** = 65536 编码器 counts（1 脉冲 = 20.48 counts）。

| 场合 | 单位 | 例子 |
|---|---|---|
| 所有运动命令（`motor_pos` / `motor_goto` / `motor_angle`） | 脉冲 | `motor_goto 4 640` |
| `limits.json` / `prep_pose.json` / `tray_calib.json` / 示教 / 序列 | 脉冲（文件里写 `"unit": "pulse"`） | `"y_pulses": -13144` |
| 界面「回读」 | 脉冲为主，括号附 counts 与角度 | `回读：-13144 脉冲（-269189 counts，-1478.7°）` |
| 固件回读 `counts`（`#0,tele` / `motor_query`） | counts | `counts=-269189` |
| 固件容差 `goto_tol_counts` / `angle_tol_counts` | counts（名字带 counts） | `200` / `500` |

换算只在 `host/units.py` 里做；文件里若写 `"unit": "counts"`，读取时自动换算成脉冲并在日志说明。
**界面禁止再把 counts 显示成「脉冲」**——历史上就是这个坑，害得标定数据一半脉冲一半 counts。

## 6. 自测（改完代码建议跑）

```bat
selftest.bat            :: 全部 7 步（约 60 秒）
selftest.bat /quick     :: 跳过 GUI 截图
```

7 步内容：上位机单测（182）→ 视觉包导入 + 权重 → 起假派 → 派端自测（24）→ 派↔真上位机协议（37）→ 笔记本 TCP 端到端（5）→ GUI 冒烟截图。

**真机闭环自测**（烧完新固件、接好回读线之后跑；它只动你指定的那根轴）：

```bat
.venv\Scripts\python.exe tools\goto_check.py --port COM6 --axis 4 --pulses 3200 --rounds 3
```

判读：每次输出 `ok err=<残余 counts> passes=<补了几轮>`；`err` 应在 `goto_tol_counts`（默认 200）以内。
失败码：`E14` 没到位 / `E11` 没接回读 / `E10` 先点「清除停止锁存」/ `E01|E02` 固件是旧的。

---

## 7. 常见问题（每条都能对症）

| 现象 | 原因 / 怎么办 |
|---|---|
| 下拉框里没有"派 …" | 派没开机 / 不在同一 WiFi / 派端服务没起。先 `ping <派IP>`，再 `systemctl status robotpi`；也可以手填 `tcp://<派IP>:5058` |
| 连接后有弹窗"连接失败" | 弹窗里写了下一步：查服务、查防火墙（`net/firewall-allow.ps1` 需管理员）、查端口 |
| 弹窗写"桥正被另一台上位机占用" | 5058 是独占控制口：把另一台电脑上的 GUI/脚本关掉，或等 10 秒自动释放；只想看回包请连只读观察口 5054 |
| 想确认独占/观察口是否正常 | `.venv\Scripts\python.exe tools\bridge_owner_check.py --host <派IP>`（5 项检查，含"第二连接被拒""观察口只读"） |
| 点连接"没反应" | 现在不会静默失败了；若真没弹窗，看「原始日志」页 |
| 画面黑 / 报"相机 N 未配置" | 派端相机槽位和 `vision/config.json` 的 `cam_map` 不一致。用 `net/find_pi.py <IP> --full --cam 0/1` 看哪个槽位是顶视 |
| 帧率只有 5–10 fps | 两台相机挂同一个 USB2 hub，带宽不够。把一台插到派的 USB3 口 |
| 日志刷"相机 N 连续抓帧失败" | USB 带宽不足（没走 MJPG）。`pi_agent` 已默认 MJPG；仍失败就降分辨率 |
| 重连后运动命令被 `E10` 拒 | 断线 3 秒固件会自动锁存停止；上位机连上后会自动 `stop_clear`，也可手动点「清除停止锁存」 |
| 四轴 `motor_query` 全 `E04` | 编码器回读线（PA3）没接，属于已知硬件缺口，不影响发命令 |
| 夹爪不回到 150° 预备位 | 固件是旧的，重烧 `stm32/hex/STM32F429.hex` |
| 双击 run.bat 报 `'m' is not recognized` | 这文件被另存成了 UTF-8/LF。本包里的 .bat 都是纯 ASCII+CRLF，改动时别破坏 |

更多：`docs/比赛现场操作卡.md`（10 条故障速查 + 赛前 10 分钟清单）。

---

## 8. 安全边界（这三层别拆）

1. **界面**：`停止全部 (Esc)` 立刻停车、清空中断所有队列；
2. **派端**：串口桥 1.5 秒收不到笔记本字节，就自动往 STM32 发四轴停车帧；
3. **固件**：3 秒收不到上位机心跳，自动锁存停止（要运动得先 `stop_clear`）。

---

## 9. 版本与已知限制

见 `VERSION.md`。最要紧的三条：

- 视觉 SAM-B 在笔记本 CPU 上约 0.3–1 s/帧，GPU 上更快；**不要**想着在派上跑 SAM（实测 20–30 s/帧）；
- 传动是**开环**的：没有编码器回读，接近段/回初始位都是"按记录重放"，误差随次数累积，靠人工归位；
- 相机槽位、分辨率一改，标定全部作废，必须重做。
