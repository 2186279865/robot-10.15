# STM32 固件主工程导读（ghr/10.15）

> 只读通读 + 交叉核对构建产物/构建日志/测试脚本，未修改任何工程文件。
> 标注：**【代码事实】**＝可在源码/产物逐行验证；**【文档声称】**＝仅写在 .md 里、未在代码中体现或与代码不一致。
> 行号基于工作目录 `D:\study\project\robot-competition` 下的相对路径。

---

## 1. 一句话定位

`ghr/10.15` 是 **STM32F407VGTx + 标准外设库（StdPeriph，非 HAL）的四轴抓取机器人固件**：主程序 `MY/main.c` 通过
**USART2(PA2/PA3)** 向 4 台 **EMM V5 步进驱动器**（Z=1、托盘=3、X=4、Y=5）发 8~13 字节指令帧；通过
**USB OTG FS CDC（板载 Type-C，PA11/PA12）**与 PC 上位机跑自定义 **ASCII 行协议**（`$id,cmd,args*CS\n`）；舵机夹爪走
**TIM4/PB6 PWM**；另有一支单独烧录的硬件体检固件 `MY/main_diag.c`。工程用"新模块全部内联进头文件"的方式回避 Keil 工程文件被覆盖（§5）。

---

## 2. 目录边界：哪些是真工程、哪些是参考

| 路径 | 作用 | 参与编译 |
| --- | --- | --- |
| `ghr/10.15/MY/` | `main.c`、`main_diag.c`、`stm32f4xx_it.c`、`stm32f4xx_conf.h` | 是（两套固件各取一支 main） |
| `ghr/10.15/Drive/Include/` | 模块接口 + **6 个 `*_inline.h` 实现** | 是（被 main.c include） |
| `ghr/10.15/Drive/Source/` | `delay.c led.c servo.c host_usb.c` 参与；`gripper.c stepper.c` 是参考副本 | 部分 |
| `ghr/10.15/USB/` | ST USB Host-Device Lib **V2.2.0**（原样未改）+ 本工程 4 个 App 文件 | 是（`USB/README.md:34-40`） |
| `ghr/10.15/Project/` | `STM32F407.uvprojx`、`STM32F407_diag.uvprojx`、`Objects/`（axf/hex/构建日志） | — |
| `ghr/10.15/Test/host_protocol/` | 主机侧协议一致性测试 | 否（PC） |
| `ghr/10.15/上位机/` | Python tkinter + pyserial 上位机（不在本报告范围，仅作交叉验证一端） | 否 |
| `ghr/10.15/STM32/` | 厂商 CMSIS + StdPeriph 源码 | 只编 7 个外设/核心 .c |

**【代码事实】** 正式工程文件清单只有：`main.c`、`stm32f4xx_it.c`、`stm32f4xx_conf.h`、`led.c`、`delay.c`、`servo.c`、`host_usb.c`、
`startup_stm32f40_41xxx.s`、`system_stm32f4xx.c`、`misc.c`、`stm32f4xx_{rcc,gpio,usart,tim}.c` + 9 个 USB 库文件（`Project/STM32F407.uvprojx:388-513`）。
**没有** `stepper.c`、`gripper.c`、`key.c`、`stm32f4xx_dma.c`——这是理解整个工程结构的钥匙。

---

## 3. 两支固件与真实启动流程

### 3.1 `MY/main.c`（正式固件，186 行，GBK 编码）

**【代码事实】include 顺序即架构分层**（`MY/main.c:1-22`）：① `stm32f4xx.h` + 接口头 `led/delay/gripper/key/stepper.h`；
② **模块实现内联** `gripper_inline.h`、`key_inline.h`、`stepper_inline.h`（`MY/main.c:8-13`，注释："本工程不把单独的 .c 加进 Keil 工程，所以在这里内联进来"）；
③ **通信三层** `host_transport.h`（接口）→ `control_inline.h`（统一控制状态+非阻塞下发）→ `host_protocol_inline.h`（行协议），注释强调
"三者都靠 `Stepper_Millis()` 的毫秒时基，主循环里不许出现阻塞等待"（`MY/main.c:15-22`）。

**【代码事实】启动顺序**（`MY/main.c:95-114`）：`Delay_Init/LED_Init/Key_Init/Stepper_Init`（Stepper_Init 装 TIM2 1ms + USART2）→
`HostLink_Init()`（只做板级初始化，不等枚举）→ `host_protocol_init()` → `control_gripper_init()`（PB6/TIM4 上电停在松开位）→
`led_blink(1)`（闪 1 下=程序活到主循环）→ `0E52` 清堵转 → `FE 98` 停车 → `F3` 使能 Z → `Delay_ms(500)`
（照"例36"上电顺序，见 `MY/main.c:28`、`108-114`）。

**【代码事实】主循环在做什么**（`MY/main.c:120-185`，一轮≈1ms）：`ms=Stepper_Millis()` → `HostLink_Poll()`（USB 枚举/复位/拔线）→
`host_protocol_task()`（收字节→分帧→分发→应答→事件→遥测）→ `Key_Poll(ms)` 按**一路状态机**分支（PA0 只管 Z 轴，`MY/main.c:53`）：
已锁存且无轴运行→`control_clear_stop()`；有轴在运行→`control_latch_stop(ms)`（`:137-143`）；未锁存空闲→
`control_apply_speed(1,正向,param speed_rpm,param acc)` 连续转（`:144-153`）；LED1(PC13) 运行常亮、空闲 60ms 亮/1200ms 灭做心跳（`:160-182`，
宏 `MY/main.c:55-57`）；`Key_TaskDelay()` 维持 1ms 节拍（`:184`）。

**【代码事实】"PC 与按键共用一套状态"的取舍**写在文件头（`MY/main.c:24-51`）：去掉上一版"自动换档阶梯"（只发不收的权宜做法）、
去掉每 250ms 重发 F6、时间基准从"ms += 常数"改为实测、下发统一走 `control_apply_*`。这是全工程**唯一**解释"为什么不靠补发帧凑动作"的地方。

### 3.2 `MY/main_diag.c`（诊断固件，119 行，GBK）

**【代码事实】** `MOTOR_ADDR 1`、`PULSES_PER_REV 3200`、每次走半圈、5 档参数表 `{60,1}{30,1}{120,1}{60,0}{300,1}`（`MY/main_diag.c:35-53`）；
上电闪 **2 下**与正式固件区分（`:84`）；循环里每档发 **FD 位置模式**相对半圈（`Stepper_MoveRelativeAt2`，`:105-106`）→停车→闪 2 下→停 2.5s（`:93-117`）。
**【文档声称/设计意图】** 位置模式而非速度模式，因为例36 实测过、且只发一次由驱动器走完，不受"加速度档太大导致起转力矩不足"影响；
"电机跟着节奏一动一动=驱动器/电源/共地/地址/接线全部 OK；全程静止=硬件或接线问题"（`MY/main_diag.c:19-32`、`交接文档-20261016.md:133`）。
它不含 USB/协议，体积仅 3416 字节 Code（§11）。

---

## 4. 硬件资源与中断优先级

| 资源 | 用途 | 证据 |
| --- | --- | --- |
| `PA2/PA3` USART2 115200 8N1 | PA2→驱动器 RXD 发指令；PA3←驱动器 TXD 回读（**只有 Z 轴接了线**） | `Drive/Include/stepper_inline.h:12-13`、`79-115` |
| `PA0` | 就地按键（内部下拉、高电平有效、20ms 消抖、长按不连发） | `Drive/Include/key_inline.h:26-44`、`47-82` |
| `PB6` TIM4 CH1 50Hz 500~2500us | 舵机夹爪 PWM | `Drive/Source/servo.c:26-46`、`49-56` |
| `PC13` | LED1（低电平点亮） | `Drive/Include/led.h:8-16` |
| `PA11/PA12` OTG FS | 板载 Type-C → CDC 虚拟串口 → 上位机 | `USB/README.md:46-54` |
| TIM2(1ms)/TIM4(PWM)/SysTick(Delay_ms) | 三个互不复用的时基 | `stepper_inline.h:12-13` |

**【代码事实】中断优先级**：TIM2=1、USB OTG FS=2、**USART2=3**（`stepper_inline.h:106-110`、`USB/README.md:54`）。
`MY/stm32f4xx_it.c` **只定义**内核异常（NMI/HardFault/…/SysTick，`MY/stm32f4xx_it.c:34-122`）；`TIM2_IRQHandler`/`USART2_IRQHandler`
由 `stepper_inline.h` 提供、`OTG_FS_IRQHandler` 由 `host_usb.c` 提供——无重复定义，但**删掉这些 inline 头就会缺中断入口**。

---

## 5. `*_inline.h` 方案：为什么实现写在头文件里

**【代码事实】原因**（`stepper_inline.h:1-9`、`gripper_inline.h:3-6`）："Keil 一直开着本工程，它内存里的文件列表是旧的，外部改 .uvprojx
会被它保存时覆盖（**已出现两次**），所以新加的 .c 无法参与编译。把实现放进已在包含路径（`..\Drive\Include`）里的头文件，由 main.c 带入，就不用动工程文件。"

| 头文件 | 参与编译 | 内容 |
| --- | --- | --- |
| `stepper_inline.h`（491 行） | ✅ `main.c:13`/`main_diag.c:11` | EMM V5 收发 + TIM2 时基 |
| `key_inline.h`（88 行） | ✅ `main.c:12` | PA0 消抖状态机 |
| `gripper_inline.h`（97 行） | ✅ `main.c:11` | **阻塞版**夹爪（旧接口，含 `Delay_ms(200)`） |
| `control_inline.h`（≈361 行） | ✅ `main.c:21` | 四轴统一状态 + 非阻塞发帧 + 夹爪等待 |
| `host_protocol_inline.h`（**1918 行/58KB**） | ✅ `main.c:22` | ASCII 行协议全套 |
| `emm_reply_inline.h`（369 行） | ✅ 被 host_protocol 包含 | 回包解析 + 非阻塞查询事务 |
| `host_transport.h`（66 行） | 仅接口 | 实现在 `Drive/Source/host_usb.c` |

**【代码事实】参考副本已漂移**：`Drive/Source/stepper.c` 文件头自述"当前工程【没有】加入 Keil、【也没有】编译它"（`Drive/Source/stepper.c:1-10`），
且 `STEPPER_RX_SIZE 32U` 与 inline 版 `64U`（`stepper_inline.h:23`）已不一致——**读代码必须以 `*_inline.h` 为准**。`gripper.c` 同理不参与；`key.c` 根本不存在
（`key_inline.h:9` 提到它是"以后若正式拆分"的计划）。
> 代价：① main.c 一个翻译单元吞掉约 3000 行实现，编译错误定位差；② 每个 `static` 状态只能一份；③ 但正因"头文件即实现"，PC 侧测试才能直接编译真实源码（§10）。

---

## 6. 步进驱动层：EMM V5 串口帧

**【代码事实】发送帧格式** `[地址][功能码][参数…][0x6B]`，**大端**，帧尾固定 `0x6B`（不是 CRC），115200 8N1。

| 命令 | 长 | 布局 | 代码 |
| --- | --- | --- | --- |
| F3 使能/失能 | 6 | `addr F3 AB on 00 6B` | `stepper_inline.h:183-188` |
| FD 位置模式 | 13 | `addr FD dir rpmH rpmL accel pulse31..0 raF snF 6B` | `stepper_inline.h:190-210` |
| F6 速度模式 | 8 | `addr F6 dir rpmH rpmL accel snF 6B` | `stepper_inline.h:212-219` |
| FE 立即停止 | 5 | `addr FE 98 00 6B` | `stepper_inline.h:221-228` |
| 0E52 清堵转 / 0A6D 清零 | 4 | `addr 0E 52 6B` / `addr 0A 6D 6B` | `:324-329` / `:315-322` |

**【代码事实】** `Stepper_SendFrame()` 逐字节等 TXE、再等 TC，两级超时各 10ms，超时返回 0（`stepper_inline.h:154-181`）；
地址表 `Z=1/TRAY=3/X=4/Y=5`（`Drive/Include/stepper.h:25-28`）、默认 `SPEED_RPM 300`、`ACCEL 128`、`PULSES_PER_REV 3200`（16 细分，`stepper.h:38-41`）、
`DIR_POSITIVE 0/NEGATIVE 1`（`stepper.h:50-51`）；多轴发送是"四帧连发再统一等一次"（`stepper_all_addr[]`，`stepper_inline.h:392-396`、`:426`）。
**【代码事实】回读只有 Z 轴一路**：四路推挽 TXD 不能直接并联，需二极管线与/1k 限流/模拟开关/RS-485（`stepper_inline.h:15-17`、`host_protocol_inline.h:49-55`）。
`Stepper_LoopbackTest()` 把 PA2 临时切成带上拉 GPIO 输入检测外部拉低，再发 `0x55,0x56` 试探回环，返回 `1=回环成功/2=线被拉低无回显/0=无反应`
（`stepper_inline.h:230-292`）——**唯一不依赖驱动器就能验证 PA2/PA3 通路的自检**。

### 6.1 回包解析（`Drive/Include/emm_reply_inline.h`）

**【代码事实】** 回包 `[addr][func][data…][0x6B]`；已知长度：`0x1F=5` 版本、`0x35=6` 转速（符号+2 字节幅值）、`0x36=8` 编码器位置（符号+32 位计数，
**65536 计数=1 圈**）、`0x3A/0x3B=4` 状态、`0xFD=4` 驱动器主动"到位"通知、`F3/F6/FE/0A/9A/9C=4` 确认；**未知功能码逐字节丢弃、不猜不拼**（`emm_reply_inline.h:6-19`、`56-69`）。
`EmmParser_Feed()` 滑动窗口：地址不符/长度未知/末字节非 0x6B 就整体右移一字节重同步（`:77-137`）。
**【代码事实】"位置≠脉冲"**：0x36 是编码器计数（65536/圈），位置指令用脉冲当量（本工程 3200/圈），两者仅当位置基准有效才对应，故界面只显示角度/计数
（`:17-19`）；角度换算 `counts*3600/65536` 定点（`:195-200`）。
**【代码事实】非阻塞查询 `EmmQuery`** 同时只允许一个（`EmmQuery_Start` active 时拒绝，`:259-283`）；`EmmQuery_Poll` 仅收本事务类型，
**地址对但功能码不对→`bad_reply`（协议 E05）** 且不提前结束（`:286-351`）；`EmmQuery_Expired` 只累加 `timeout_count`、**不清旧读数**（`:354-367`）。

---

## 7. 控制层（`Drive/Include/control_inline.h`）

**【代码事实】** 四轴表 `{1,3,4,5}`（`control_inline.h:50`）、每轴 `ControlAxis{addr,enabled,dir,acc,rpm,mode,pos_valid,task}`（`:38-48`）、模式三态 `STOP/SPEED/POS`（`:33-35`）。
- **锁存**：停车不是一次发完，而是置锁存+记账，由 `control_service()` 每拍只发一帧、四轴发完清 active，帧间隔用 `frame_gap_ms`（`:142-158`、`:325-345`）；
- **非阻塞位置**：`control_apply_position(...,wait_ms=0)` 只发一次 FD 帧、**不等**（`:219-251`），所以"已发送/驱动器已应答/实测到位"三级证据在协议层分开上报；
- **无基准保护**：绝对模式且 `pos_valid==0` 返回 2 → 协议回 **E13**（`:219-247` + `host_protocol_inline.h:1276-1288`）；运动中的 `motor_zero` 返回 2 → **E09 moving**（`:280-300`）；
- **夹爪**：`control_gripper_init()` 上电直接把 PB6 输出到 **180°（松开位）**（`:122-127`、初始值 `:63`）；`control_gripper_apply()` 记目标角+busy+ready 时刻，
  到点置 `settled`，上层只报一次 `settle_elapsed`（`:302-322`、`:325-361`）——**"等待结束"≠"夹到位"**。
- 分工：`gripper_inline.h` 是**阻塞版**（每步 `Delay_ms(200)`，`gripper_inline.h:19-23`），PC 控制路径**不走**它，全部走 `control_inline.h`。

---

## 8. 上位机协议层（`Drive/Include/host_protocol_inline.h`，1918 行，本工程最重要的文件）

### 8.1 帧格式（**【代码事实】**，`:6-24`）

```
请求  $<id>,<cmd>[,<arg>...]*<CS>\n     例：$12,motor_speed,1,0,60,1*4A
应答  #<id>,ok[,k=v...]*<CS>\n          错误 #<id>,err,<code>[,k=v...]*<CS>\n
事件  #0,event,<type>[,k=v...]*<CS>\n   遥测 #0,tele,...*<CS>\n
```
- **CS**＝起始符之后、`*` 之前所有 ASCII 字节**异或**，两位**大写**十六进制（`:965-976`）；整帧 **≤96 字节**（payload≤91，`HOST_MAX_LINE_CHARS 94`，`:36-47`）；
- 超长→弃到 LF 回 **E06**；校验错→**E07**（`:1619-1633`、`:1636-1682`）；
- **`!` 是带外紧急停止字符**：USB OUT 回调与协议层都会拦截，置 `stop_pending` 并作废半帧（`host_usb.c:190-195`、`host_protocol_inline.h:1757-1762`）。

### 8.2 错误码（**【代码事实】**，`:57-70`）

| 码 | 含义 | 例 |
| --- | --- | --- |
| E01 | 未知命令 | `:1587-1591` |
| E02 | 参数错（`reason=argc/range/speed_limit/mode/what/action/unknown_param/out_of_range`） | `:1007-1048`、`:1208-1214` |
| E03 | 轴非法（只接受 1/3/4/5） | `:1018-1036` |
| E04 | 查询超时（驱动器没回） | `:741-748` |
| E05 | 回包类型错（地址对、功能码不对） | `:751-758` |
| E06 | 帧超长 | `:1621-1633` |
| E07 | 校验和错 | `:1676-1682` |
| E08 | 使能失败 | **定义后从未使用（死码）** |
| E09 | 忙（`query_busy`/`moving`） | `:1392-1397`、`:1345-1355` |
| E10 | 已锁存（须先 `stop_clear`） | `:1179-1183`、`:1215-1219` |
| E11 | 该轴无回读能力 | `:1380-1386` |
| E12 | 同 id 不同内容 | `:1702-1716` |
| E13 | 无位置基准却要绝对定位 | `:1278-1283` |

### 8.3 运行参数（13 个，**【代码事实】**，`:72-117`）

`speed_rpm 60[1..6000]`、`acc 1[0..255]`、`speed_rpm_max 300`、`gripper_open_deg 180`、`gripper_grab_deg 135`、`gripper_step_deg 45`、
`gripper_settle_ms 200`、`heartbeat_ms 500`、`link_timeout_ms 3000`、`query_timeout_ms 100`、`telemetry_ms 200`、`frame_gap_ms 10`、`readback_mask 15`；
`host_protocol_init()` 上电恢复出厂值（`:1800-1819`）。**注意 `heartbeat_ms` 定义但无人读取**——心跳实际用 `link_timeout_ms`（`:1866-1876`）。

### 8.4 命令集合（19 个，**【代码事实】**，派发表 `:1582-1617`）

`hello ping status motor_enable motor_speed motor_pos motor_stop stop_all motor_stall_clear motor_zero motor_query
gripper(open|grab|step_grab|step_release) servo_angle param_get param_set tele_on tele_off stop_clear`。
- **`hello(id,proto,session)`**：proto 必须=1 否则 `E02 want/got`；成功清会话、清查询、`ready=1`、`boot_id++`、`owner="pc"`，
  回 `fw=0.1.0 proto=1 boot=xxxxxxxx axes=1/3/4/5 readback=<按 mask 生成>`（`:1082-1124`）；
- **`motor_speed`**：超 `speed_rpm_max`→`E02 speed_limit`；锁存中→`E10`；成功回 `ok,addr,rpm,task` 并排 `event task state=sent`（`:1195-1230`）——**ok≠已动**；
- **`motor_pos`**：`mode=rel|abs`，abs 无基准→`E13`（`:1232-1295`）；
- **`motor_query(addr,vel|pos|flag)`**：名单外→`E11`；已有查询在途→`E09 query_busy`；**应答被延后**：先记 `defer_id/addr/what`，等回包或超时后补发
  `#id,ok,addr=,counts=,deg=/rpm=/flag=,age_ms=`（`:1361-1406`、`:712-758`）——**唯一异步应答的命令，PC 侧必须容忍延迟**；
- **`gripper`/`servo_angle`**：目标角夹在 `[gripper_grab_deg,gripper_open_deg]`（`:1408-1487`）；**`tele_on/tele_off`**：单轴订阅 20~60000ms（`:1541-1567`）。

### 8.5 会话去重与"先 ok 后 event"（**【代码事实】**）

4 个会话槽（`:121-132`）：**同 id+同 payload 只重发原应答、不重复执行**（对相对位移至关重要）；同 id 不同内容→`E12` 且**不覆盖**原缓存；
`hello` 永远跳过旧缓存（否则拔插后重新握手被上一次会话挡成 E12）（`:1636-1740`）。命令处理函数**只能排队事件**（单槽），由 `host_handle_line` 在应答后 flush，
保证线序"先 ok 后 event"（`:911-952`、`:1734-1739`）。

### 8.6 主状态机 `host_protocol_task()`（**【代码事实】**，`:1821-1901`）

1. 取传输层锁存的 `!`；2. 取链路 RX 错误（**注释仍写"等 host_usb.c 接上后上报 link 事件"，但 host_usb.c 早已存在——注释过期**，`:1834`）；
3. `control_service(now,frame_gap_ms)` 推进错峰停车+夹爪 settled 事件；4. 每轮最多收 **64 字节**（保证按键/停止响应）并刷新 `last_rx_ms`；
5. `host_query_service()` 解析 PA3 回包→补发延后应答/超时 E04；6. 停止字符→未锁存则 `control_latch_stop`+`event latch set stop_char`；
7. 心跳：`now-last_rx_ms > link_timeout_ms`→`link_lost=1`、`owner="none"`、锁存停车、`event link timeout`，**只停车不关 Z 使能**（防垂直机构下滑，`:1866-1876`）；
8. 遥测：订阅生效时每周期"轮询一个接回读的轴 + 订阅轴与其余回读轴各发一帧 tele"（`:1878-1900`、`:806-834`）。

### 8.7 防混淆设计（最值得借鉴的工程习惯）

**【代码事实】** 每轴独立缓存 `HostAxisRead{have,what,counts,rpm,flag,stamp_ms}`，**绝不用 A 轴读数报 B 轴**（`:618-629`）；"本次新数据"要求
`stamp_ms>=sent_ms`（`:675-682`）；`tele` 里 `rpm` 是**命令值**，`readback=1` 才附 `rpm_measured/flag/counts+deg` 并带 `age_ms`，`readback=0` 就只写 `readback=0`（`:870-909`）。

---

## 9. USB 传输层（`Drive/Source/host_usb.c` + `USB/`）

**【代码事实】** 链路＝板载 Type-C → OTG FS Device → CDC → PC 虚拟 COM（`host_usb.c:1-27`）。本工程只写 3 个文件：`usb_conf.h`、`usb_bsp.c`（PA11/PA12、时钟、DWT 微秒延时）、
`host_usb.c`；ST 库 V2.2.0 原样未改（`host_usb.c:5-8`、`USB/README.md:34-40`）。
- **发送不另建缓冲**：直接写 CDC 类自己的 `APP_Rx_Buffer`（2048B）环形队列，由库在 SOF 中断里每 5 帧搬一次；`HostLink_QueueTx()` 先算剩余空间，
  放不下就**整体拒绝**、绝不写半帧，指针更新用 `__disable_irq()` 短临界区（`host_usb.c:375-427`）；
- **接收**：CDC OUT 回调复制进 512B 环形队列，回调内不做阻塞操作（`:180-211`）；队列满记 `RXERR_OVERRUN`；
- `HostLink_Poll()` 兜底：`device_status != USB_OTG_CONFIGURED` 时清空收发队列（`:325-333`）；VID `0x0483`/PID `0x5740`（`USB/README.md:53`）。
- `host_transport.h` 只有 7 个函数（`host_transport.h:26-66`），返回码 `OK/ERR_PARAM/ERR_NOT_READY/ERR_FULL`、错误位 `OVERRUN/FRAMING`、容量 512——抽象干净，才使 PC 桩件测试成立。
- `USB/App/_st_eval_reference/` 是 ST 评估板原版参考，**不参与编译**（`USB/README.md:31-32`）。

### 9.1 USART2 vs USB：谁对上位机

**【代码事实】** 两条链路完全分开：**USART2(PA2/PA3)→只接 EMM V5 步进驱动器**（发指令/收 Z 轴回读），**不是**上位机串口；
**USB CDC(PA11/PA12)→只与 PC 跑 ASCII 行协议**。
`上位机方案.md:36`（文档）曾把「例35 的 USART1 控制台 PA9/PA10」当作可复用的"已验证通信链路"，但本工程**没有用 USART1**，
最终实现走 v3 方案的 USB CDC——**方案演进后的真实结果，读旧文档易误判**。

---

## 10. 测试层（`Test/host_protocol/`）：主机侧协议一致性测试

**【代码事实】** 思路：**在 PC 上直接编译真实固件源码**（`host_protocol_inline.h`+`control_inline.h`），只把硬件与 USB 换成桩：
`host_test_stubs.h` 用 `#define` 把 `Stepper_*`/`Servo_*` 映射到 `HostTest_*`（`Test/host_protocol/host_test_stubs.h:1-33`），
`test_host_protocol.c` 定义 `HOST_PROTOCOL_HOSTTEST` 让 `control_inline.h` 走桩件分支（`test_host_protocol.c:24-25`）。
**跑法**（`Test/host_protocol/run_host_tests.cmd:18-38`，四步）：①`python -m ziglang cc` 编译（本机无 MSVC/gcc）→ ②跑 `test_host_protocol.exe` 并把固件
**真实发出的每一帧**写进 `frames.txt` → ③`check_frames.py` 用**上位机自己的** `上位机/protocol.py` 逐帧再解析（跨实现交叉验证）→ ④用**真实 ARMCC V5** 重编同一翻译单元。
**`frames.txt` 是什么**：不是输入而是**输出快照**——`HostLink_QueueTx` 实际发出的整帧（含 LF），如 `#1,ok,fw=0.1.0,proto=1,boot=00000001,axes=1/3/4/5,readback=1/3/4/5*6E`。
交叉验证规则：任一帧解析失败/校验和不符/错误码不在 E01~E13/长度>96 都算失败，且必须出现 `ok/err/event/tele` 四类帧、总数≥20（`check_frames.py:34-62`）。
**【代码事实】覆盖用例**（`Test/host_protocol/README.md:33-53`）：hello/rehandshake、ping/status、文档逐字节验过的 `$12,motor_speed,1,0,60,1*4A`、去重、错误码全家桶、
位置非阻塞与 E13、锁存与错峰停车、`!` 停止字符、分帧（残帧/超长/96 字节边界/粘包/拆分）、参数、夹爪 135°≠0°、上电必须 PB6=180°、遥测周期与"不声称到位"、
3 秒失联、PA3 回读全链路（3 字节询问帧、负数、超时 E04 不编数）、`readback_mask` 多轴轮询不串台。
**【文档声称】** `USB/README.md:71` 说协议层"跑通 **238 项检查**"——数字来自 1241 行的断言计数，本报告未逐条复核。
> 最值得抄的做法：把协议层从硬件里剥出，用真实源码+桩件做字节级回归，再用上位机解析器二次校验。局限：**验证的是协议行为，不是硬件**（`README.md:61-62`）。

---

## 11. Keil 工程与构建产物

**【代码事实】** `Project/STM32F407.uvprojx`：TargetName `STM32F407`（`:10`）、Device `STM32F407VGTx`（`:17`）、编译器 ARMCC **V5.06 update 6 (build 750)**（`:13`）、
`Optim=1` 即 -O1（`:317`）、宏 `USE_STDPERIPH_DRIVER,STM32F40_41xxx`（`:341`）、包含路径含 `..\MY`+`..\Drive\Include`+4 个 USB 路径（`:343`）、
**输出名 `OutputName=STM32F429`（`:52`）**。
> **怪点**：芯片是 F407VG、工程名 STM32F407，但产物叫 `STM32F429.axf/.hex`——历史遗留，烧录看 `Project/Objects/STM32F429.hex`，容易误认成另一块板。

**【代码事实】构建结果**（`Project/Objects/`、`Project/build_*.log`）：最新全量重编 `Rebuild target 'STM32F407'` →
`Program Size: Code=27728 RO-data=808 RW-data=516 ZI-data=7108`、`0 Error(s), 0 Warning(s)`（`Project/Objects/STM32F429.build_log.htm:53-55`，时间戳 2026/10/21 20:00）；
诊断固件 `Code=3416 RO-data=448 RW-data=68 ZI-data=1092`、0 error（`STM32F429_diag.build_log.htm:32-34`）；
历史快照 12 份：`build_official.log` Code=3308（USB 改造前）、`build_usb.log` Code=23644（USB 接入后）、`build_rb4.log`/`build_posmode.log` Code=27728（与最新一致）。
产物：`STM32F429.axf`(629616B)、`STM32F429.hex`(80998B)、`STM32F429_diag.axf/hex`。
**【文档声称】** `交接文档-上位机开工-TypeC-20261016.md:266`：旧 Code=3308/3044 是 USB 改造前；地址表改后"分别单独编译 main.c/main_diag.c 成功；**没有重新完整链接、没有烧录、没有验证 USB**"。
该说法在 10/16 成立，**10/21 20:00 已发生一次完整重编+链接（含 host_usb.c）**——两者不矛盾，只是状态已推进。

---

## 12. 已实现 vs 尚未在硬件上验证（重要）

### 12.1 已验证到什么程度

| 范围 | 证据等级 |
| --- | --- |
| EMM V5 帧格式/回包长度表/解析器 | **例36 实测过**（`交接文档-20261016.md:17,268,282`；`emm_reply_inline.h:1-6`） |
| 按键 PA0 控制 Z 轴 + 驱动器/电源/共地/地址/接线 | **文档声称已实测好用**（`交接文档-20261016.md:17,28`） |
| 夹爪 180°松/135°夹、PB6 方向 | **文档声称已验证**（`交接文档-上位机开工-TypeC-20261016.md:66`、`上位机方案-v2.md:41`） |
| 行协议全量行为 | **PC 桩件 + 上位机解析器双向验证**（`Test/host_protocol/README.md:33-53`） |
| 全工程编译链接 | **0 error 0 warning**，Code=27728（`STM32F429.build_log.htm:53-55`） |

### 12.2 明确尚未验证（**【文档声称】**，与代码状态一致，`USB/README.md:67-74`）

1. **USB CDC 从未上板、从未枚举**；已验只有编译、符号表存在（`OTG_FS_IRQHandler`/`USBD_Init`/`USBD_CDC_cb`/`HostLink_*`/`host_protocol_task`）、协议层 PC 跑通。
   首次上板要确认：Windows 能否枚举虚拟 COM、`hello` 能否握手、拔插重连。CC1/CC2 悬空时需 USB-A 转 Type-C 线（`:73-74`）。
2. **PA3 回读全链路（真驱动器回包）**：只验证过"帧能编出来、解析器能被桩件喂进去"，真实 TXD 电平/共地未验（`stepper_inline.h:15-17`）。
3. **四轴联动**：`control_service()` 错峰发帧只有桩件覆盖，没有任何"四个电机同时转"的记录。
4. **夹爪在 PC 协议路径下的方向/到位**：只报 `settle_elapsed`，不报"夹到"；夹持力/打滑无数据。
5. **心跳失联自动停车**：只在测试里用假时间验过，未在真拔线场景验证。

**【代码事实】间接线索**：`main.c:46-50` 与 `main_diag.c:28-32` 都留着"PA0 不动就按 4 条硬件清单排查"的说明，说明作者**至少遇到过一次电机不转**；
而 `交接文档-20261016.md:17` 明确写「例36 按键控制 Z 轴，**实测好用**」。稳妥结论：**"例36 那套 FD/F6 发帧在本硬件上转过（Z 轴）"有文档证据，
但 10.15 这套固件（USB+四轴+回读）没有整机上电记录**；诊断固件的作用正是把该不确定性收敛成"一动一动=硬件 OK"。

---

## 13. 文档声称 vs 代码事实：漂移清单

| # | 说法 | 代码事实 | 位置 |
| --- | --- | --- | --- |
| 1 | "步骤 1 里所有轴都没有回读能力" | 已实现 `readback_mask`+0x36/0x35/0x3A 解析+四轴轮询 | `host_protocol_inline.h:22-24` vs `:618-834` |
| 2 | "链路错误等 host_usb.c 接上后上报" | host_usb.c 已在工程里，链路错误只清计数 | `:1834` vs `host_usb.c:432-438` |
| 3 | `heartbeat_ms` 参数 | 无逻辑读取，心跳实际用 `link_timeout_ms` | `:90` vs `:1869` |
| 4 | E08 使能失败码 | 定义后从未使用 | `:65` |
| 5 | "没有重新链接/没有烧录" | 10/21 已完整链接出 axf/hex | `交接文档-上位机开工-TypeC-20261016.md:266` vs `Objects/STM32F429.build_log.htm:53` |
| 6 | `Drive/Source/stepper.c` 是"实现" | 不参与编译且已漂移（RX_SIZE 32 vs 64） | `stepper.c:1-10,15` vs `stepper_inline.h:23` |
| 7 | 旧方案把 USART1 当上位机链路 | 上位机走 USB CDC，USART2 只接驱动器 | `上位机方案.md:36` vs `main.c:15-22`、`host_usb.c:1-8` |
| 8 | `queued` 字段如实上报队列 | 恒为 0（发送队列未落地） | `host_protocol_inline.h:1165-1166` |

---

## 14. 与上位机联调的最小接口清单

- **连接**：USB CDC 虚拟 COM（**不是** USART2）；115200 只是形式值，首次需自动绑 `usbser`。
- **握手**：`$1,hello,1,<session>*CS` → 必须见 `ok,fw,proto,boot,axes=1/3/4/5,readback=…`；proto≠1 回 `E02 want/got`。
- **运动**：`motor_speed/motor_pos/motor_stop/stop_all/stop_clear`；**ok≠已动**，完成证据看 `#0,event,task,state=sent`。
- **回读**：只有 `readback_mask` 里的轴能查，否则 `E11`；`motor_query` 应答延迟 ≤ `query_timeout_ms`（默认 100ms）。
- **安全**：`!` 单字节急停；`stop_all` 置锁存后运动命令全回 `E10`，须先 `stop_clear`；3 秒无字节自动锁存停车（不关使能）。
- **遥测**：`tele_on(addr,interval)` 后默认 200ms 一帧；`rpm` 是命令值，`readback=1` 才有实测值。

---

## 15. 阅读建议（按重要性）

1. `Drive/Include/host_protocol_inline.h`：1~120 行（设计契约）→ 1582~1740（分发/去重）→ 1821~1901（状态机）；
2. `MY/main.c` 全 186 行（尤其 24~51 取舍注释、95~185 主循环）；
3. `Drive/Include/control_inline.h` 142~361（锁存/非阻塞/夹爪等待）；
4. `Drive/Include/stepper_inline.h` 154~344（组帧+发送超时+回环自检）；
5. `Test/host_protocol/run_host_tests.cmd` + `check_frames.py`（怎么证明协议是对的）；
6. `USB/README.md`（USB 移植的坑与未验证项）。
