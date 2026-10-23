# 02 PC 上位机（ghr/10.15/上位机）调研报告

> 范围：`ghr/10.15/上位机/`（11 个 py + 6 个测试 + 2 份 json + logs）+ `ghr/10.15/` 下 5 份方案/交接文档 + 固件端 `ghr/10.15/Drive/Include/host_protocol_inline.h` 对照。
> 方式：只读通读 + 实跑测试；全部结论标注 `路径:行号`。注意：工作区里 `上位机/{host_gui,host_client,calib_dialog,calib_wizard}.py` 与 4 个测试文件相对 HEAD 有未提交改动（`git status`），本报告描述的是**工作区当前版本**。

## 1. 一句话定位

一套**纯标准库 Python + tkinter 的桌面上位机**：把 PC 行协议（`$...#CS\n`）经 USB CDC 虚拟串口发给 STM32，由固件转发 EMM 帧驱动 Z/托盘/X/Y 四轴与舵机夹爪；PC 侧负责协议编解码、心跳/重连、手动控制、参数在线读写、动作序列编排回放、视觉目标导入与"视觉 u/v → X/Y 脉冲"标定。**它不直接对驱动器说话**，这是三版方案唯一没变的核心决定（`ghr/10.15/上位机方案.md:22`、`上位机方案-v2.md:76`、`上位机方案-v3-TypeC-CDC.md` 传输层改 USB 但架构未变）。

## 2. 文件清单与规模

| 文件 | 行数 | 职责 |
|---|---|---|
| `ghr/10.15/上位机/protocol.py` | 548 | 帧文本语法、XOR 校验、错误码表、命令参数规格（ArgSpec）、分帧缓冲、会话去重、请求 id 池 |
| `ghr/10.15/上位机/serial_link.py` | 251 | 串口抽象：pyserial / Windows `\\.\COMn` 文件句柄双后端 + 端口枚举 + 指数退避重连器 |
| `ghr/10.15/上位机/host_client.py` | 478 | 统一客户端：握手、心跳、超时重发、事件队列、后台服务线程 |
| `ghr/10.15/上位机/simulator.py` | 513 | 无硬件模拟固件（同一套协议），时间推进产生 tele/task 事件 |
| `ghr/10.15/上位机/sequence.py` | 337 | 动作序列数据模型 + 执行器状态机（ok≠到位） |
| `ghr/10.15/上位机/vision_grasp.py` | 266 | 视觉目标 → X/Y 脉冲 → 生成 Sequence（全流程编排） |
| `ghr/10.15/上位机/vision_import.py` | 183 | GUI「视觉目标」页；只读导入 new_eye 视觉结果，不运动 |
| `ghr/10.15/上位机/host_gui.py` | 1088 | 主窗口、6 个分页、顶栏、软位置记账、一键回 0 |
| `ghr/10.15/上位机/calib_wizard.py` | 528 | 标定数值核心（解 6 未知仿射、残差、三套标定模式），无 Tk |
| `ghr/10.15/上位机/calib_dialog.py` | 783 | 标定向导 Tk 对话框（发运动命令、串流程） |
| `ghr/10.15/上位机/tests/*.py` | 424/343/277/277/207/157 | 6 个测试文件，共 121 个用例（实跑确认） |

数据文件：`tray_state.json`（64 B，仅 `{"current_box": 1, ...}` 记忆转盘当前盒）、`vision_grasp.json`（1130 B，示教量）、`logs/readback-20261017-174030.jsonl`（85 行真机抓包）。

## 3. 分层架构与线程/队列模型

```
Tk 主线程(界面)            host_client 服务线程            link 线程/模拟器
  after(30ms) _pump ──► poll_events(≤300) ──► deque(maxlen=1000) ◄── _dispatch(帧)
        │  Runner.notify(reply/err/timeout/event)               ▲
        │  client.send()（只在界面线程调用）                      │
        └──────────────► _write → link.write ──────────► 串口/SimulatedLink
```

- 分工写死在 `ghr/10.15/上位机/host_gui.py:1-13` 的 docstring：serial_link 只做字节进出；HostClient 把字节变成状态与事件；**界面线程只 `after()` 轮询事件，绝不从后台线程碰控件**；SequenceRunner 由 GUI 驱动。
- 唯一后台线程在 `ghr/10.15/上位机/host_client.py:443-478`：`start_service()` 起 daemon 线程，`_service_loop()` 每 20 ms `link.poll()` → `link.read()` → `feed_bytes()` → `tick()`；异常不抛，只 emit + `handle_link_lost()`。`stop_service()` join 超时 1.0 s（`:450`）。
- 事件总线是 `deque(maxlen=1000)`（`ghr/10.15/上位机/host_client.py:94-132` 初始化），`poll_events(limit)` 在 `:142`；`UiEvent.kind` 取值 state/rx/tx/reply/err/event/tele/note/timeout（`:70-76`）。
- 界面轮询在 `ghr/10.15/上位机/host_gui.py:957-967`：`reconnector.tick()` → `poll_events(300)` → `_on_event()` + `runner.notify()`（只喂 reply/err/timeout/event）→ `runner.tick()` → `_refresh_state()` → 各面板 `refresh_readback()` → `after(POLL_MS=30)`（`:52`）。
- 关闭顺序在 `ghr/10.15/上位机/host_gui.py:1059-1069`：在线先 `stop_all()` + sleep 0.1 s，再 disarm、disconnect、`stop_service()`、destroy。

## 4. 协议层 protocol.py（代码事实）

- 帧语法写在 `ghr/10.15/上位机/protocol.py:1-19`：请求 `$<id>,<cmd>[,<arg>...]*CS\n`，成功 `#<id>,ok[,k=v...]*CS\n`，失败 `#<id>,err,<code>[,k=v...]*CS\n`，事件 `#0,event,<type>,...`，遥测 `#0,tele,...`；`CS` = 起始符与 `*` 之间所有 ASCII 的**逐字节 XOR**，两位大写十六进制。
- 常量：`PROTOCOL_VERSION = 1`（`protocol.py:30`）、`MAX_FRAME_BYTES = 96` / payload 91（`:38-39`）、`VALID_AXES = (1, 3, 4, 5)`（`:46`）、`AXIS_NAMES = {1:"Z",3:"托盘",4:"X",5:"Y"}`（`:47`）。
- 错误码 13 条 E01–E13（`protocol.py:51-65`）：E01 未知命令、E02 参数越界、E03 地址越界、E04 驱动器无回包、E05 回包格式错、E06 行过长、E07 校验错、E08 未使能就运动、E09 运动中不允许的操作、E10 停止锁存中、E11 该轴未接回读、E12 请求 id 冲突、E13 位置基准无效。
- 证据等级 `sent/driver_ack/reached/settle_elapsed`（`protocol.py:68-71`），事件类型 `task/latch/link/reset`（`:73`）——这是整套"ok≠到位"约定的落点。
- `PARAM_DEFAULTS` 13 项（`protocol.py:76-92`）：speed_rpm=60、acc=1、speed_rpm_max=300、gripper_open_deg=180、gripper_grab_deg=135、gripper_step_deg=45、gripper_settle_ms=200、heartbeat_ms=500、link_timeout_ms=3000、query_timeout_ms=100、telemetry_ms=200、frame_gap_ms=10、readback_mask=15。
- 编解码：`xor_checksum` `:111`、`build_line` `:149`、`encode_request/ok/err/event/telemetry` `:156/161/166/173/178`。
- 解析：`Frame` 数据类 `:202`、`parse_line` `:250`（逐项校验起始符、`*`、校验和、帧长 ≤96、id 数字、请求 id≠0、err code 已知、id=0 只允许 event/tele）、`FramingBuffer` `:309-367` 逐字节喂、LF 分帧、丢弃 `$` 之前的垃圾字节、超长丢弃并计数、ascii(replace) 容错。
- 命令表 `COMMAND_SPECS`（`protocol.py:393-430`）用 `ArgSpec`（`:376`）声明参数范围与枚举，`validate_request` `:448` 做统一校验 —— 与固件共用一份"规格即文档"。
- 会话去重 `SessionCache` `:488-524`（容量 16，三态 NEW/DUPLICATE/CONFLICT，同 id 同内容→重发上次应答）；`RequestIdPool` `:527-548`（1..65535 递增，不回收）。

### 4.1 命令表（`protocol.py:393-430` 声明，`:448` 统一校验）

| 命令 | 参数（ArgSpec 范围） | 说明 |
|---|---|---|
| `hello` | proto 1..65535, session 0..0xFFFFFFFF | 开会话、作废旧 id、回 fw/boot/axes/readback |
| `ping` | 无 | 心跳，回 `ms` |
| `motor_enable` | axis, on 0/1 | 使能/取消使能 |
| `motor_speed` | axis, dir 0/1, rpm 1..6000, acc 0..255 | 连续转 |
| `motor_pos` | axis, dir, rpm, acc, pulses ≥0, mode rel/abs | 位置模式（方向与脉冲数分离，见 §12.5） |
| `motor_stop` / `stop_all` | axis / 无 | 单轴停 / 全部停 |
| `motor_stall_clear` | axis | 解堵转 |
| `motor_zero` | axis | 位置清零 |
| `motor_query` | axis, what vel/pos/flag | 等回读的查询（唯一会延迟应答的） |
| `status` | 无 | latch/owner/running/queued/gripper 快照 |
| `gripper` | open/grab/step_grab/step_release | 舵机夹爪 |
| `servo_angle` | 0..270 | 直接给角度 |
| `param_get` / `param_set` | name / name+value −1e6..1e6 | 参数在线读写 |
| `tele_on` / `tele_off` | axis, interval 20..60000 | 遥测订阅 |
| `stop_clear` | 无 | 解停止锁存（不恢复运动） |

## 5. 与固件端 host_protocol_inline.h 的对应关系（实测：一一对应）

| 维度 | PC 侧 | 固件侧 |
|---|---|---|
| 协议版本 | `protocol.py:30` = 1 | `host_protocol_inline.h:36` `HOST_PROTO_VERSION 1U` |
| 帧上限 | `protocol.py:38` 96 字节 | `host_protocol_inline.h:37-38` 96/91 |
| 错误码 | `protocol.py:51-65` E01–E13 | `host_protocol_inline.h:58-70` 同名同码 |
| 参数 13 项默认值 | `protocol.py:76-92` | `host_protocol_inline.h:87-105` 逐项相同（含 grab=135、heartbeat=500、readback_mask=15 及 1..15 范围） |
| 命令名 | `COMMAND_SPECS` `protocol.py:393-430` | 派发表 `host_protocol_inline.h:1582`、名字表 `:1595-1612` |
| hello | 发 `proto,session`（`protocol.py:393` 起） | `host_cmd_hello` `host_protocol_inline.h:1082`，回 `fw/proto/boot/axes=1/3/4/5/readback` `:1116-1123` |
| 同 id 去重 | `SessionCache`（16 槽） | `host_session_find` `:1700`；同内容重发原应答 `:1704-1711`；冲突回 E12 且不覆盖缓存 `:1713` |
| hello 例外 | 客户端 `begin_handshake` 每次换新 session（`host_client.py:172`） | hello 一律不看旧缓存，注释解释"否则重连误回 E12" `host_protocol_inline.h:1691-1697` |
| 查询延迟应答 | 客户端等 reply 配对（`host_client.py:303`） | `host_defer_*` 等驱动器回包再补发 ok `host_protocol_inline.h:591-597`、`:1728-1739` |
| 回读名单 | `readback_axes`（hello 解析） | `host_readback_mask()` `:602`、`host_axis_has_readback` `:607` |
| `!` 停止 | GUI 禁止输入框发 `!`，用顶栏按钮 `host_gui.py:694` | `host_feed` 见到 `'!'` 直接置停止（帧内帧外都算）`host_protocol_inline.h:1757-1762` |
| 残帧恢复 | `FramingBuffer` 以 `$` 重同步 | 收到 `$` 无条件重启本行 `host_protocol_inline.h:1748-1755` |

发现的一处**实现差异（非协议差异）**：会话缓存槽 PC 侧 16（`protocol.py:488`）而固件 4（`host_protocol_inline.h:44` `HOST_SESSION_SLOTS 4U`），故客户端在大轮次循环中重发较早请求时，固件会"忘了"该 id 而重新执行 —— 对幂等命令无影响，对相对位移命令理论上存在重复执行窗口（代码未做防护）。传输层另有 `ghr/10.15/Drive/Src/host_usb.c`（12560 B）与 `Drive/Include/host_transport.h`（3564 B），与 `host_protocol_inline.h` 组成 CDC 通路。

**真机证据**：`ghr/10.15/上位机/logs/readback-20261017-174030.jsonl` 第 4 行起是真实固件应答 `fw=0.1.0 proto=1 boot=00000001 axes=1/3/4/5 readback=1/3/4/5`，证明 PC 解析器能吃掉固件产出的帧；但同文件 3 轮 × 4 轴 × 3 种 what 的 `motor_query` 全部 `E04` —— 四轴回读线在真机上没通。该文件全仓库无生成代码（grep `readback-\*.jsonl` 无命中），是一次性诊断脚本产物。

## 6. 链路层 serial_link.py

- 双后端：`HAVE_PYSERIAL` 探测（`ghr/10.15/上位机/serial_link.py:16-24`），有 pyserial 用 `SerialLink`（`:81`，115200，`in_waiting` 或 1 字节读）；没有则 `WinFileLink`（`:135`，`os.open("\\\\.\\COMn")`，读用 `STREAM_TIMEOUT_S=0.05`）。**没有 pyserial 也能跑模拟后端**（README 明确承诺）。
- 端口枚举 `list_serial_ports` `:52`（pyserial 缺失时给 COM1..COM20 手填），`PortInfo` `:35-49` 用 `STM32_VID=0x0483`（`:27`）标 "STM32/其他"，`sort_ports` `:72` 把 STM32 排前。
- 重连 `AutoReconnector` `:199-251`：基 1000 ms、上限 8000 ms、`base*2^min(attempts,3)` 退避，只在 `state=="link_lost"` 时动作，重连后 attach + connect。

## 7. 客户端 host_client.py（状态机 + 策略）

- 常量：`HEARTBEAT_MS=1000`（`host_client.py:24`）、`LINK_TIMEOUT_MS=3000`（`:25`）、`TIMEOUT_QUERY_MS=300` / `TIMEOUT_DEFAULT_MS=600`（`:27-28`）、幂等可重试集合 `RETRYABLE={"ping","status","motor_query","param_get","hello"}`（`:30`）。
- 状态机 `disconnected/connecting/handshaking/online/link_lost`（`:48-60`）；结果码 `WAITING/OK/ERR/TIMEOUT/UNKNOWN`（`:62-66`）。
- 握手 `begin_handshake` `:172`：清空 `boot_id`、`readback_axes`、换新 id 池、随机 session，发 `hello(proto=1,session)` 超时 1000 ms；`_apply_ok` `:334` 解出 fw/boot/axes/readback → 置 online。
- 重发 `resend` `:236-248`：仅 RETRYABLE 且 attempts<3 才重发；否则 `motor_pos` 这类落到 `RESULT_UNKNOWN` 并附"结果未知，不要盲目重发"文案（`:388-416` 的 `tick()`）。
- 心跳/失联 `tick()` `:388`：每 `HEARTBEAT_MS` 发 ping；`now-last_rx_ms > LINK_TIMEOUT_MS` → `handle_link_lost("心跳超时")` `:425`，置 link_lost、所有 pending 判 UNKNOWN、禁下发。
- 停止锁存：`stop_all` → `_apply_ok` 见到 `ok` 置 `latched=True`（`:334` 段）；`stop_clear` 带 `latch=0` 才解锁；锁存期间运动命令由固件回 E10，GUI 提示"需先清除停止锁存"。
- `motion_hook`：`send()` `:205-234` 在发出 `motor_pos/motor_speed/motor_zero/stop_all/stop_clear` 时回调，供 GUI 维护**软位置**（见 §9）。

## 8. 模拟器 simulator.py

- 自我声明"模拟数据，不是实测数据"（`ghr/10.15/上位机/simulator.py:1-8`），只保证协议一致、不保证时间特性。
- `FIRMWARE_VERSION="sim-0.1"`（`:18`）、`BOOT_ID=0x5A5A0001`（`:19`）、回读名单由 `readback_mask` 推导（`:23-27`）、`COUNTS_PER_REV=65536`（`:30`）。
- 协议面全覆盖：`feed` 处理 `!` 锁存（`:100-117`）、`handle` 里 **hello 不做会话去重**（`:125`）、DUPLICATE 重发、CONFLICT 回 E12、`validate_request` 参数校验；`_LATCH_EXEMPT={"hello","ping","status","stop_clear"}`（`:158`）。
- 关键命令：`_cmd_hello` `:160`（版本不符 E02 带 want/got）、`_cmd_motor_pos` `:225`（abs 且该轴无回读 → **E13**）、`_cmd_motor_query` `:268`（无回读 → E11；pos 回 counts 与 1 位小数 deg）、`_cmd_gripper` `:289`、`_cmd_stop_clear` `:343`（只解锁不恢复运动）。
- 时间推进 `advance_to` `:381-410`：速度模式累加、位置模式到点发 `task state=reached`、夹爪 settle 发 `state=settle_elapsed`、按周期发 tele；`SimulatedLink.poll` `:494` 调 `advance_to`+`drain_events`，让"随时间产生的字节"真的能读出来。另有 `simulate_disconnect` `:505`、`simulate_mcu_reset` `:509` 供 GUI/测试造故障。

## 9. GUI（host_gui.py，6 个分页）

顶栏（`host_gui.py:405-441`）：串口下拉 + 刷新 + 「模拟后端」勾选 + 连接/断开 + 一键回 0 + 固件/状态标签 + 红色「停止全部 (Esc)」+「清除停止锁存」+ 最后通信时间。

| 分页 | 注册位置 | 功能要点 |
|---|---|---|
| 四轴控制 | `host_gui.py:448` | 4 个 `AxisPanel`（2×2，`:74`）：方向/转速/加速度速度模式；位置模式"转几圈 + 转几度"自动折算脉冲（`total_pulses` `:191`，1 圈=3200 脉冲，1°≈8.889）；下发方式 rel/abs（`send_pos` `:252`，圈度同时为 0 不下发，abs 且无位置基准先弹确认）；停止/使能/取消使能/解堵转/位置清零；查速度/位置/状态；订阅遥测（`:318`）+ 回读显示（`refresh_readback` `:345`，非回读轴显示"该轴未接回读"，>3 s 显示"数据已过期"） |
| 舵机夹爪 | `:456` | 夹紧/松开/收紧一步/松开一步 + 指定角度；文案写死"135°=夹紧（180-45，不是 0 度）180°=松开"（`_build_gripper` `:475-510`）；明示"状态：软件目标值，无到位传感器" |
| 参数 | `:460` | 13 个参数中文名 + 读取/写入；`readback_mask` 写入后提示并自动重新握手（`send_param_set` `:821-833`）；自定义参数名/值 |
| 动作序列 | `:464` | 动作下拉、参数、完成条件 ok/ms/reached、等待/超时、超时跳过→添加；表格增删/上下移/清空/保存/加载 json；运行/暂停/继续/中止（`seq_*` `:848-952`） |
| 视觉目标 | `:467-469` | `from vision_import import build_tab`（**无 E402 免疫，局部 import 避免启动即依赖 new_eye 文件**），见 §11 |
| 原始日志 | `:472` | 自动滚动/清空/保存 + 调试模式十六进制原始字节发送（`send_raw` `:835`，禁止 `!`）；日志上限 2000 行（`:53`、`_log` `:1033`） |

交互细节：Esc 绑「停止全部」（`_build_top` `:405-441`）；日志 Text 用 tag 上色——发送 #0a4a8a、接收 #444、提示 #8a5a00、错误 #b00020、事件 #0a6b0a、遥测 #666（`_build_log` `:617-645`）；`_on_event` `:969-999` 对 `hello` 应答让所有面板 `reset_reference()`（重新握手 = 四轴位置基准全作废），对 E13/E04 有专门中文文案；`_refresh_state` `:1001-1028` 刷连接色、固件版本+启动标识、收发计数、坏帧/超长计数、锁存状态。

软位置记账（`host_gui.py:710-747`）：`motor_pos` rel 累加、abs 置未知、`motor_zero` 置 0、`motor_speed`/`stop_all` 置未知（停止 5 s 内视为可能在滑行）。一键回 0（`:749-805`）按 `home_order=[1,4,5,3]`（`:43`）逐轴发 rel 回到最近一次位置清零点，未知位置的轴跳过，发前弹确认框，等待时间按 rpm 估算。

## 10. 标定向导（calib_wizard.py + calib_dialog.py）

- 数学模型 `PX = a*u + b*v + tx`、`PY = c*u + d*v + ty`，3 个不同 (u,v) 对应点解 6 未知数；反解与残差写在 `new_eye/tray_calib.py`，本模块只管流程与检查（`ghr/10.15/上位机/calib_wizard.py:1-18` docstring）。
- 纯函数可测：`counts_to_pulses` `:58`、`pick_pulses` `:69`（回读过期→拒绝，不拿旧读数冒充）、`wrap_delta` `:94`（65536 计数回绕）、`hand_points` `:105`、`measure_points` `:215`（脉冲差→位移）、`check_points` `:233`（u/v 最小间距 `MIN_UV_DELTA=0.01`）、`solve` `:281`（可 write+backup，`_backup` `:317`）、`solve_ortho` `:368`、`solve_hand` `:327`、`solve_pulse` `:361`、`report` `:422`、`summary` `:470`、`load_marks/save_marks` `:480/507`、`marks_points` `:518`。
- 三种模式（`calib_dialog.py:164` step、`:198` marks、`:164-197` push；`on_mode` `:228`）：
  - **marks（默认，`calib_dialog.py:49`）**：读 new_eye 当前 (u,v) → 走一个 X 向已知脉冲 → 记录点 → 走回起点 → 走 Y 向 → 记录 → 求解；数据存 `new_eye/runtime/calib_marks.json`（`calib_wizard.py:33`）。
  - **push**：顶推平台"手推到第一个点→记录→推回→…"；`on_push_record` `:717`、`on_push_solve` `:764`。
  - **step**：`on_read/on_record/on_solve` `:557/560/643`。
- 对话框流程：`on_jog(axis,sign)` `:287` 发单轴相对位移、`_report_jog` `:318` 报每步等时，`on_query_counts` `:374` 查回读、`_current_pulses` `:408` 由软位置+回读推当前脉冲、`_home` `:427` 走回 0、`_read_uv` `:452` 读视觉中心、`on_marks_solve` `:547` 求解、`on_solve_ortho` `:675` 用最新视觉反推正交性、`open_wizard(app)` `:782` 由 GUI 惰性调用（`vision_import.py:160`）。
- 写回位置：`ghr/10.15/上位机/calib_wizard.py:31` `CALIB_PATH = ROOT/new_eye/tray_calib.json`，与 new_eye 共用一份标定文件（不是上位机私有）。

## 11. 视觉目标导入链路（只读）

- `ghr/10.15/上位机/vision_import.py:21-23`：`ROOT = Path(__file__).resolve().parents[3]`（→ 仓库根），`TARGET = ROOT/new_eye/runtime/latest_targets.json`，`CALIB = ROOT/new_eye/tray_calib.json`；`MAX_AGE_S=8.0`（`:26`）。
- `read_targets` `:29-54` 的硬校验：文件不存在 → "先在 new_eye 里点单次识别或开实时分割"；`schema_version` 必须 ∈ {4,5}；**`motion_authorized` 必须为 false**（视觉侧无论如何都不会授权运动）；ts 缺失、超 8 s 过期、`center_tray` 非 0..1 都报错。→ 视觉与运动是**单向数据文件**解耦，PC 不可能被视觉侧反向驱动。
- `load_calib` `:57`：缺 `affine` 直接 ValueError（没标定就不给生成序列）。
- `build_tab` `:67`：树表列 id/cls/u/v/views/flags（height 12、多选）；`refresh` `:89` 打风险标记（need_review→待登记、views=top→副摄未印证、side→副摄多出、color_mismatch→两路颜色不一致），状态行显示批次/年龄/数量/schema/路径；`generate` `:122` 需先选中行 → `vision_grasp.build_sequence` → warnings 前 8 条 → askyesno 确认（步数与起止盒）→ 记 `tray_state.json` → 填进动作序列页，**不自动运行**（`:155` 附近 note）；`reset_state` `:171` 把转盘记忆重置为盒 1。
- 生成规则在 `ghr/10.15/上位机/vision_grasp.py:170-266`：每目标 12 步左右（Z 安全高度→X→Y→Z 降→夹紧→Z 抬→X/Y 回放料位→转盘转到类别盒→Z 降→松开→Z 抬），脉冲由 `_pulse_from_calib` `:145` 套仿射得到，转盘角度取最短路径 `_tray_delta` `:121`（>180° 折返），盒号由 `class_to_box` 映射 `_box_of` `:100`；参数全在 `vision_grasp.json`（`DEFAULTS` `vision_grasp.py:33-56`：rpm 40/acc 30、xy timeout 9000、z safe/grasp/lift/place 均 0=未示教、盒角 [0,60,…,300]、gripper settle 500/400）。`z.safe==0` 等未示教会进 warnings。
- 结论：任务描述里"导入 Qwen 的 vision_targets.json"在**上位机代码中不成立** —— 全目录 grep `vision_targets` 仅命中 `test_vision_grasp.py:115` 的临时测试文件；上位机只认 new_eye 的 `latest_targets.json`。Qwen 那是另一条链路（`new_eye` 侧或 `Qwen/` 脚本）。

## 12. 动作序列 sequence.py

- 12 种步骤 `STEP_KINDS`（`ghr/10.15/上位机/sequence.py:28-41`），参数顺序与协议一致，`Step` 数据类 `:44` 含 kind/args/wait/wait_ms/timeout_ms/on_timeout/note；`validate` `:56-79` 检查未知动作、轴白名单、`wait=reached` 必须有回读轴、时间参数为正。
- 三种完成条件 `WAIT_OK/WAIT_TIME/WAIT_REACHED`（`:20-22`），超时策略 abort/continue（`:24-25`）。
- `Sequence` `:104-133` 存/读 json（utf-8、indent 2）；执行器状态 IDLE/RUNNING/WAITING/PAUSED/DONE/ABORTED（`:140-145`）。
- `SequenceRunner` `:148-337`：`start` `:181` 先整体校验、`pause/resume` `:194/200`（resume 不重发当前步）、`abort` `:207`、`tick` `:216`（WAITING 超时→"等到位超时"/"等待固件应答超时"）、`notify` `:230-268`（按 req_id 配对；只有 `event type=task state=reached` 且 addr 匹配才算到位）、`_advance` `:276`、`_next` `:312`、`_fail` `:329`。
- 这就是"ok 只代表接纳"的最终落点：`motor_pos` 的 ok 只让序列进入 WAITING，没有 reached 就靠 `wait_ms` 兜，绝不假装到位。

### 12.5 代码注释里记录的真实踩坑（可当验收清单）

1. **脉冲数不能带负号**：`motor_pos` 的 `pulses` 下限是 0，方向必须走 `dir` 参数；`vision_grasp.py:135` 的 `_motor()` 用 `direction = pos_dir if pulses>=0 else 1-pos_dir` 统一折算，`vision_grasp.py:1-17` 明说"E02 就是这么踩的"。
2. **相对位移重发会重复执行**：靠会话缓存同 id 同内容只回放不重跑（`protocol.py:488-524`、`host_protocol_inline.h:1704`），所以客户端只对幂等命令重试（`host_client.py:30`）。
3. **改 `readback_mask` 必须重新握手**：GUI 写入后主动 `begin_handshake()` 并提示（`host_gui.py:821-833`），否则航向按旧名单回 E11/E04。
4. **无回读轴的 abs 位置会被固件拒**（E13）：模拟器 `simulator.py:225` 复现该行为，GUI 在 `send_pos` `:252` 先弹"位置基准未知"确认框。
5. **夹爪没有到位传感器**：只有 settle 时间（`host_gui.py:475-510` 文案），显示的是软件目标值；`stop_clear` 也只解锁不恢复运动（`simulator.py:343`）。
6. **停止后锁存**：运动命令一律 E10，必须顶栏「清除停止锁存」（`host_client.py:334` 段、`protocol.py:51-65`）。
7. **重新握手/重连会作废四轴位置基准**（`host_gui.py:969-999` 收到 hello 就 `reset_reference()`），所以断线重连后先回 0 再干活。

## 13. 测试：怎么跑、覆盖什么、实际通过数

- 命令（`ghr/10.15/上位机/README.md:112-123`）：`cd ghr\10.15\上位机` → `python -X utf8 -m unittest discover -s tests`。
- 覆盖：`test_protocol.py` 分帧/校验/超长恢复/会话去重/id 池；`test_client.py` 握手/心跳/超时/去重/坏帧/失联；`test_sequence.py` 序列模型/校验/执行器状态机含到位；`test_gui_smoke.py` **真建 Tk 窗口**跑模拟后端（`test_gui_smoke.py:1-5` 说明；无 tkinter `:31` 跳过，无显示 `:42-43` 抛 SkipTest；`:48-55` 用 `Args(sim=True,autoconnect=True)` 起 App 并 pump 1.2 s）；`test_calib_wizard.py`、`test_vision_grasp.py` 覆盖标定与序列生成。
- **实跑结果（本次调研执行）**：`Ran 121 tests`，`FAILED (errors=1)`；唯一错误是 `test_sequence.SequenceModelTest.test_save_load_roundtrip` 写 `C:\Users\...\Temp\...\seq.json` 时 `PermissionError` —— 属**本沙箱对临时目录的写限制**，非代码缺陷；其余 120 个用例通过。
- **文档与代码不一致**：`ghr/10.15/上位机/README.md:13` 声称"75 个自动化测试全绿"，实际收集到 **121** 个用例（README 计数已过时，但"全绿"结论未被证伪）。

## 14. 三版方案演进与代码实际落地

| 版本 | 传输层 | 关键主张 | 落地情况 |
|---|---|---|---|
| `ghr/10.15/上位机方案.md`（422 行，v1） | **USART1 / PA9(TX) PA10(RX) + USB 转 TTL**（`:22`、`:72` 要求 3.3V 电平） | "PC 不直接对驱动器发帧"；移植 `xrw/例35` 的 USART1 控制台（`:36`、`:399`）；文件结构含 `protocol.py`+`emm.py`（`:286-288`）；5 个分页（`:301`） | 架构被继承；USART1 路线被 v3 废弃；`emm.py` 未实现 |
| `ghr/10.15/上位机方案-v2.md`（308 行） | 仍是 USB 转 TTL / USART1（`:13`、`:150`） | 补齐非阻塞主循环、停止锁存、遥测限流、`Drive/Include/host_uart_inline.h`（`:188`） | 其中的协议/停止/失联设计成为代码事实；文件名与 v3 不同 |
| `ghr/10.15/上位机方案-v3-TypeC-CDC.md`（约 298 行） | **Type-C USB OTG FS → CDC 虚拟串口**（PA11/PA12 DP/DM），`host_usb.c`+`host_transport.h`+`host_protocol_inline.h`（`:188-196`） | 夹爪 135=夹紧（`:11`）、96 字节帧（`:102`）、E01–E13（`:140`）、5 个分页（`:172`）、`:296-298` 自述"只完成方案修订与源码核对，未下载 USB 库、未编译 USB 栈、未上板验证" | **代码落地到本版**：`README.md:3` 声明依据 v3、`:5` 明确"不是 USB-TTL"；GUI 标题即 `机器人抓取系统 上位机（Type-C USB CDC）`（`host_gui.py:384`） |

v1/v2 遗留但没进代码的东西：`config.json`（v1 `上位机方案.md:290`，`交接文档-上位机开工-TypeC-20261016.md:177` 仍列着；实际被 `vision_grasp.json` + `tray_state.json` 取代）、`emm.py`、参数持久化、"曲线面板"（v1 `:321` 第二期，未做）。反过来的**代码超出文档**：v3 只规划 5 个分页，代码多出「视觉目标」页与 `vision_grasp/vision_import/calib_wizard/calib_dialog` 四个模块（v1-v3 三份方案均无标定向导设计）。

## 15. 文档声称 vs 代码事实（不一致清单）

1. **测试数**：README "75 个全绿"（`README.md:13`）vs 实际 121 个用例（120 通过 + 1 沙箱权限错误）。
2. **分页数**：`README.md:6` 列 5 页（四轴/夹爪/参数/序列/日志）vs 代码 6 页，多「视觉目标」（`host_gui.py:467-469`）。
3. **夹爪夹紧角**：早期文档写 0°——`交接文档-上位机开工-TypeC-20261016.md:60` 声称"夹紧 0°"；代码与 v3 均为 **135°=180-45**（`protocol.py:76-92` `gripper_grab_deg=135`、`host_gui.py:475-510` 文案、`host_protocol_inline.h:93`）。以代码/固件为准。
4. **"上位机仅有方案未写代码"**：`交接文档-20261016.md:77`、`:284`（"PC↔单片机协议设计中未实现"）已被后续提交推翻——现工作区有 4000+ 行实现且能上板握手。
5. **上位机方案里"命令分发层约 300 行"**（`交接文档-20261016.md:334`、`上位机方案.md:62`）vs 实际 `host_protocol_inline.h` 1918 行（含 EMM 转发/回读/遥测/会话缓存），量级严重低估。
6. **四轴回读"已完成"叙事**：README 状态表把四轴回读列为可配（`readback_mask` 默认 15），但同文档承认"硬件并线未验证"，且真机日志全 E04（§5）——代码侧支持 ≠ 硬件已通。
7. **`vision_targets.json`（Qwen）**：任务描述与部分文档暗示上位机导入它；代码只读 `new_eye/runtime/latest_targets.json`（`vision_import.py:22`）。
8. **序列/参数持久化**：README 未做清单（`:169-181`）与代码一致——`config.json` 不存在，序列只能手工存 json。

## 16. 当前完成度与未验证点

已完成（代码事实）：
- 协议层/链路层/客户端/模拟器全链路自洽，121 用例（120 通过）覆盖；`--sim` 无硬件可全流程点一遍。
- 真机：CDC 枚举 + hello 握手 + 心跳已跑通（日志 `readback-20261017-174030.jsonl`）；Z 轴回读曾单独跑通（`README.md` 状态表、`交接文档-上位机开工-TypeC-20261016.md:232-234` 的"先单 Z 轴回读"策略）。
- GUI 六页、标定向导三模式、视觉目标 → 序列生成、序列回放、软位置与一键回 0 均已实现。

未验证 / 未做：
- **四轴回读硬件并线**未验证（真机日志全 E04）；多路 TXD 不能直接并联，README（`上位机/README.md:21-69`）给了二极管线与/模拟开关/RS-485 三条候选但都没落地。
- **PC 侧没有 EMM 回包解析**：v1 规划的 `emm.py`（`上位机方案.md:288`）从未实现，EMM 帧的收发/解析全在固件（`host_protocol_inline.h:581-597` 的 `EmmParser`、`Drive/Include/emm_reply_inline.h`），因此"E05 回包格式错"只可能在固件侧产生，PC 只能看到 host 层错误码。
- 绝对位置模式在固件侧形同 rel（`README.md:169-181` 自述 `control_apply_position()` 传 `absolute=0`），GUI 的 abs 语义只有前端意义。
- E08 永不发出；`readback_mask` 写入后需要重新握手才生效（`host_gui.py:821-833` 强制）。
- 视觉抓取序列**从未真机跑过**：`z.safe/grasp/lift/place` 默认全是 0（`vision_grasp.py:33-56`），未示教就只会 warning，`build_sequence` 不阻止生成；`tray_state.json` 的记忆依赖每次成功的生成-运行闭环，失败中断后盒号会漂。
- 标定文件 `new_eye/tray_calib.json` 与 `calib_marks.json` 在工作区是**未跟踪新文件**（`git status` 的 `??`），说明标定结果尚未入库、可复现性存疑。
- 未做：PyInstaller 打包、参数持久化、曲线、序列模板（`README.md:169-181`）。

## 17. 复核指引

```powershell
cd ghr\10.15\上位机
python -X utf8 -m unittest discover -s tests      # 121 用例
python -X utf8 host_gui.py --sim                  # 无硬件全流程
python -X utf8 host_gui.py --port COM5 --connect  # 真机
python -X utf8 host_gui.py --selftest             # 只建界面跑几轮事件循环
```
协议对照：`ghr/10.15/上位机/protocol.py` ↔ `ghr/10.15/Drive/Include/host_protocol_inline.h`（同源"规格即文档"，参数默认值逐项一致可直接 diff）。
