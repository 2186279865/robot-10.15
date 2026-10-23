# 05 参考示例 / 赛题规则 / 编译日志 / 工程协作规范

> 调研范围：`xrw/例35`、`xrw/例36`、赛题文本 `newpdftext.txt`+`pdftext.txt`、`EMM_V5_串口协议与工程指令总结.md`、`build/uv4_*.log`、`ghr/10.17`、`ghr/10.19`、`ghr/10.15` 的协作规范文档。
> 全部只读核查；路径均相对仓库根 `robot-competition/`；行号以本次读取的文本为准。

---

## 0. 结论摘要（先看这 8 条）

1. **赛题目的是 2027 年第十届工创赛「智能+工程创新赛道」，三个赛项：智能搬运 / 智能分拣 / 智能救援**（`newpdftext.txt:11`）。本项目（三/四轴步进机械臂 + 270° 舵机夹爪 + 双相机 + 6 工位托盘转位）形态上对应 **智能分拣**，见 §1.7 判据。
2. 智能救援**明令禁止抓取**："禁止抓取救援目标及将救援目标放置在机器人上"（`newpdftext.txt:511`），所以带机械臂的方案直接不适用救援赛项——这条足以把救援排除，不必争论。
3. 智能分拣的硬指标：外形 ≤**350×350×450mm**、六个储物盒、托盘 ≥**160×160** 且边缘 ≥10mm、场地 **500×500**、货物 ≤40mm 方形、初赛 ≥12 件 / ≥6 类、**一件分拣+显示完才可下一件**、**15 秒无动作或无显示即本轮结束**、两次机会取最好。全部出处见 §1.4。
4. EMM V5 帧格式一句话：`[地址][功能码][参数…][0x6B]`，多字节**大端**，无 CRC（`0x6B` 只是固定帧尾，源码里叫"校验字节"）（`EMM_V5_串口协议与工程指令总结.md:52`、同文件 `:60`）。本项目走 **USART2 / PA2 / 115200 8N1 / 只发不收**（`README.md:49`）。
5. 编译日志：仓库内 4 份 `uv4_*.log`，**3 份成功、1 份失败**；成功的 3 份都是 **10.17/10.19 的 ST7735S 屏工程**（`Program Size: Code=2718 RO-data=490 RW-data=24 ZI-data=1024`，0 Error 0 Warning），**不是 10.15 主固件**；唯一一份 10.15 的日志是失败的（objects 目录缺失）。详见 §4。
6. 例 36 = **按键 + Z 轴单段升降的最小闭环**（主工程的按键写法、上电 FE→F3 序列、"FD 只发一次"都来自这里）；例 35 = **视觉 XY 跟随 + Z 取放 + 托盘转位的最完整流程**（主工程运动逻辑的源头，但主工程砍掉了视觉与舵机）。详见 §3。
7. `xrw/` 下 76 个 `backup_before_*` 目录（例35: 44 个 / 例36: 32 个）是作者的实验史快照，被 `.gitignore:50` 排除，**不入库、不要读**。
8. 协作规范的核心是**三条铁律**：①`*_inline.h` 才是实现、不能删、`gripper.c/stepper.c` 不能加进工程；②别在 Keil 外改 `.uvprojx`（保存会被覆盖）；③GBK/UTF-8 混合编码不要批量转码。详见 §6。

---

## 1. 赛题：2027 工创赛「智能+工程创新赛道」

### 1.1 两份文本的关系

| 文件 | 体量 | 性质 | 用法 |
|---|---|---|---|
| `newpdftext.txt` | 556 行 | **官方规则**《附件2-1 2027年第十届中国大学生工程实践与创新能力大赛 智能+工程创新赛道竞赛命题与运行》（`newpdftext.txt:4-6`） | 尺寸/流程/判罚以此为准 |
| `pdftext.txt` | 643 行 | 于兆勤《竞赛命题解析》51 页 PPT 抽出的逐页文本 | 补评分公式、区域细节（如 `pdftext.txt:293`） |

两份冲突时**以 `newpdftext.txt` 为准**；解析稿的价值在于把计分公式写成了单行。

### 1.2 三赛项一览（含"本项目能不能报"）

| 赛项 | 一句话任务 | 关键禁止项 | 本项目契合度 |
|---|---|---|---|
| 智能搬运 | 自主导航 + 搬运 + 码垛/装配的轮式机器人（`newpdftext.txt:21-24`） | 不遮挡场地、仅允许垂直向下补光 | ✗ 无底盘/导航/避障，场地 2400×2400 级别 |
| **智能分拣** | 托盘内的货物 → AI 识别形状/颜色/文字/二维码 → 自动放进六个储物盒并显示统计（`newpdftext.txt:242`、`newpdftext.txt:253`） | 一件一显示、超 15 秒无动作即结束 | **✓ 主赛项**（托盘+夹爪+转位） |
| 智能救援 | 双车对抗，把救援目标转运进本队安全区（`newpdftext.txt:384-388`） | **禁止抓取、禁止把目标放上机器人**（`newpdftext.txt:511`） | ✗ 机械臂方案直接违规 |

### 1.3 三赛项共有的电控约束（做方案时不能违反）

- 完全自主运行，**一键启动**，不得用任何方式遥控（分拣见 `newpdftext.txt:330`、`newpdftext.txt:262`）。
- 只用**一个内置电源**，比赛过程中（含调试）**不能更换**（`newpdftext.txt:263-264`）。
- 允许与笔记本电脑通讯，但**运行中不能触碰笔记本**（`newpdftext.txt:261-262`）。
- 传感器与电机的种类、数量不限（`newpdftext.txt:260`）——这条给了"加双相机 + 四台 EMM V5"的自由度。

### 1.4 智能分拣：全部硬约束（本项目必须逐条核对）

| 约束 | 数值 / 要求 | 出处 |
|---|---|---|
| 组成 | 至少「储物盒 + 分拣机构 + 托盘」三部分，可拆分也可整体 | `newpdftext.txt:242` |
| 识别能力 | AI 识别颜色、形状、文字、图像、二维码 | `newpdftext.txt:253-254` |
| 显示屏 | **必须有**一块**仅具显示功能**的高亮屏，显示序号 / 名称 / 图片 / 分拣成功总数量 | `newpdftext.txt:257-258` |
| 分拣节奏 | 一件分拣并存好 → 屏显示本次统计 → 才能分拣下一件 | `newpdftext.txt:331-334` |
| 外形尺寸 | 长×宽×高 ≤ **350×350×450 mm** | `newpdftext.txt:270` |
| 储物盒 | **六个**独立储物盒，尺寸形状不限，需装得下不超过 4 件货物 | `newpdftext.txt:271-272` |
| 托盘 | ≥ **160×160 mm**，边缘高度 ≥ **10 mm** | `newpdftext.txt:273-274` |
| 占场地 | 500×500 mm 正方形平面区域内 | `newpdftext.txt:282` |
| 货物 | 最大在 **40mm 正方形**内；需把形状+颜色+信息完全一致的放同一盒；初赛 ≥12 件、种类 ≥6 | `newpdftext.txt:285-287` |
| 启动 | 调试结束、裁判发令后**一键启动**，计时开始 | `newpdftext.txt:330` |
| 判负 | **超过 15 秒**装置没动作或没显示本次分拣信息 → 本轮结束 | `newpdftext.txt:334` |
| 机会 | 每队两次运行，取最好成绩 | `newpdftext.txt:341` |
| 计分 | 盒中只 1 件得 2 分；同形同色多件放同盒，**每件 5 分**；错放 0 分 | `pdftext.txt:409-411` |
| 总分 | `P = A×20% + B×10% + C×70%`（A 文档 / B 创意设计 / C 现场） | `pdftext.txt:415` |

**对本项目的直接含义**：
- 六个储物盒 ↔ 现在 `TRAY_ADDRESS=3` 的 6 个角度（0/60/120/180/240/300°）。**托盘转位天然就是"选盒"机构**，这是本项目最贴合分拣的地方。
- 双相机装在托盘上方 → 对应"托盘内货物识别"，与规则的"货物放托盘里自动判别"完全一致。
- **缺件 ①：高亮显示屏**。规则是"必须"，10.17/10.19 的两个 ST7735S 工程正好是现成的显示驱动来源（见 §5），这条缺口有现成解。
- **缺件 ②：一键启动 + 15 秒节奏**。主工程现在是"PA0 多次按键 + 每个动作固定等 2.5s"，与"一键启动、一件一显示、15 秒内必须有动作/显示"存在结构性差距。
- **风险 ③：350×350×450mm**。这是最容易翻车的一条——必须实测含相机支架、Z 轴行程、托盘、六盒的整机包络；Z 轴 40 圈（`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:45`）在 3200 脉冲/圈下是 128000 脉冲，行程由丝杠/传动决定，需实测换算成 mm 再对 450mm 高度校核。

### 1.5 智能搬运（排除依据，仅作对照）

- 机器人（含机械臂）铅垂投影 ≤ **300×300mm**、高度 ≤ **400mm**，机械臂可折叠但"出发后方可自行展开"（`newpdftext.txt:45-47`）。
- 顶部必须有**静态任务码显示装置**，字高 ≥ **12mm**，亮光、不被遮挡（`newpdftext.txt:34-36`）。
- 场地 2400×2400、灰色十字车道宽 400、障碍 φ50×100 随机（`newpdftext.txt:76-77`）；物料 ABS/PLA、≤80mm、≤100g、6 种颜色（`newpdftext.txt:123`）。
- 计分同为 `P=A×20%+B×10%+C×70%`（`pdftext.txt:293`）。
→ 本项目没有底盘、定位、避障，**做不了搬运**；但"任务码显示（字高 12mm）+ 一键启动"这两条思路对分拣同样有用。

### 1.6 智能救援（排除依据）

- 双车同场，场地 3000×3000、围栏厚 <20mm 高 <100mm（`newpdftext.txt:440-443`）。
- 机器人 ≤ **1.5kg**、投影 ≤ **300×300mm**、高 ≤ **200mm**（`newpdftext.txt:409-410`）。
- 必须先单独转运 **1 个普通物资** 才可运其它；一次转运 ≤3 个；伤员必须**单独 1 个**；不得运危险目标（`newpdftext.txt:503-510`）。
- **禁止抓取目标、禁止把目标放在机器人上**（`newpdftext.txt:511`）；接触超 10 秒强制分离（`newpdftext.txt:514`）。
- 得分 5 / 10 / 15，误放扣分（`pdftext.txt:580-585`），总分公式 `pdftext.txt:596`。
→ **与机械臂方案根本冲突**，不要在导读里把它写成候选赛项。

### 1.7 赛项判定：为什么说"我们是智能分拣"

判据按证据强度排序：
1. 机械结构 = 托盘 + 分拣机构（三轴 + 夹爪 + 转位盘），这正是规则要求的三件套（`newpdftext.txt:242`）。
2. 转位盘六个角度 ↔ 规则要求的六个储物盒（§1.4 表）；代码里已有 0/60/120/180/240/300° 映射（`xrw/例35 视觉XY接近跟随(1)(2)/README.md:69-76`、`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:12` 托盘地址 3）。
3. 双相机俯视托盘 ↔ 规则"货物放于托盘内，装置自动判别"（`newpdftext.txt:255-256`）。
4. 反证：救援明文禁抓取（`newpdftext.txt:511`）；搬运需要移动底盘与 2400 场地。
→ **导读建议表述**："本项目面向智能分拣赛项，机械臂+夹爪+转位盘+双相机是分拣装置的识别与搬移机构；显示与一键启动是当前最大合规缺口。"

---

## 2. EMM V5 串口协议速查表

来源：`EMM_V5_串口协议与工程指令总结.md`（733 行，对厂家例程的逐行总结）。

### 2.1 物理层与帧格式

| 项 | 值 | 出处 |
|---|---|---|
| 电平/波特 | 115200，8N1，**无校验** | `EMM_V5_串口协议与工程指令总结.md:35-40` |
| 帧格式 | `[地址][功能码][辅助码或参数…][0x6B]` | `EMM_V5_串口协议与工程指令总结.md:52` |
| 帧尾 | 固定 `0x6B`，**不做动态 CRC** | `EMM_V5_串口协议与工程指令总结.md:45`、`EMM_V5_串口协议与工程指令总结.md:60` |
| 多字节序 | **大端**（高字节在前） | `EMM_V5_串口协议与工程指令总结.md:666` 起"参数编码规则" |
| 地址 | 1/2/3/4；0 = 广播/同步 | 同上 §四 |
| 本项目接线 | STM32 **PA2/USART2_TX → 四台驱动器 RX**，四台 TX **不接、不并联**，PA3 留空 | `README.md:49`、`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:6`（`EMM_TX_ONLY 1`） |

### 2.2 指令速查（按实用频率排序）

| 功能 | 帧（十六进制） | 长度 | 说明 / 出处 |
|---|---|---|---|
| 位置模式 | `[addr] FD dir velH velL acc clk[4] raF snF 6B` | 13B | `raF=0` 相对、`1` 绝对；`clk` 32 位脉冲；`snF=1` 等同步（`EMM_V5_串口协议与工程指令总结.md:287`、`EMM_V5_串口协议与工程指令总结.md:320`） |
| 速度模式 | `[addr] F6 dir velH velL acc snF 6B` | 8B | `vel` RPM 0~5000；`acc=0` 直接启动（`EMM_V5_串口协议与工程指令总结.md:244`、`EMM_V5_串口协议与工程指令总结.md:268`） |
| 立即停止 | `[addr] FE 98 snF 6B` | 5B | 只停车不清状态（`EMM_V5_串口协议与工程指令总结.md:344`） |
| 使能 | `[addr] F3 AB state snF 6B` | 6B | `state=1` 使能（`EMM_V5_串口协议与工程指令总结.md:218`） |
| 当前位置清零 | `[addr] 0A 6D 6B` | 4B | **不是机械回零**，只把当前计数清零（`EMM_V5_串口协议与工程指令总结.md:76`） |
| 解除堵转保护 | `[addr] 0E 52 6B` | 4B | 上电保险，主工程第一步就发它（`EMM_V5_串口协议与工程指令总结.md:96`） |
| 控制模式 | `[addr] 46 69 svF ctrl_mode 6B` | 6B | 0 关脉冲 / 1 开环 / 2 闭环 / 3 限位复用（`EMM_V5_串口协议与工程指令总结.md:191`） |
| 多机同步触发 | `[addr] FF 66 6B` | 4B | 配 `snF=1` 的 FD/F6 帧用（`EMM_V5_串口协议与工程指令总结.md:364`） |
| 设回零零点 | `[addr] 93 88 svF 6B` | 5B | 把当前位当零点（`EMM_V5_串口协议与工程指令总结.md:394`） |
| 触发回零 | `[addr] 9A o_mode snF 6B` | 5B | `o_mode` 见文档表（`EMM_V5_串口协议与工程指令总结.md:476`） |
| 中断回零 | `[addr] 9C 48 6B` | 4B | ⚠ 厂家源码发送长度写成 5，`cmd[4]` 未赋值 → 会多发一个字节（`EMM_V5_串口协议与工程指令总结.md:518`、`EMM_V5_串口协议与工程指令总结.md:526`） |
| 读实时位置 | 发 `[addr] 36 6B` → 回 `[addr] 36 dir pos[4] 6B`；`angle = raw×360/65536`，`dir=1` 取负 | 8B | `EMM_V5_串口协议与工程指令总结.md:158`、`EMM_V5_串口协议与工程指令总结.md:164` |
| 读实时转速 | 发 `[addr] 35 6B` → 回 `[addr] 35 dir spdH spdL 6B` | 6B | `EMM_V5_串口协议与工程指令总结.md:137`、`EMM_V5_串口协议与工程指令总结.md:143` |
| 到位返回 | `[addr] FD 9F 6B` | 4B | 位置模式走完自动回（`EMM_V5_串口协议与工程指令总结.md:328`） |
| 读其它参数 | 功能码 `1F/20/21/24/27/31/33/37/3A/3B/42/43` | — | 版本/阻抗/PID/母线电压/相电流/编码器/目标位置/位置误差/状态/回零状态/驱动参数/系统状态（`EMM_V5_串口协议与工程指令总结.md:115` 表） |

### 2.3 常用完整示例帧

- 速度 1000RPM、加速度档 10：`01 F6 00 03 E8 0A 00 6B`（`EMM_V5_串口协议与工程指令总结.md:688`）
- 相对位置 3200 脉冲（=1 圈，16 细分）：`01 FD 00 03 E8 00 00 00 0C 80 00 00 6B`（`EMM_V5_串口协议与工程指令总结.md:691`）
- 清零：`01 93 88 00 6B`；回零：`01 9A 00 00 6B`；停车：`01 FE 98 00 6B`（`EMM_V5_串口协议与工程指令总结.md:700-709`）

### 2.4 三份代码实现对照（同一协议、三种写法）

| 位置 | 形态 | 特点 |
|---|---|---|
| `xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/emm_protocol.c` / `例36…/HARDWARE/EMM/emm_protocol.c`（两份内容相同，3429B） | 构造器 + 解析器 | `Emm_BuildPosition` 13B（`:57`）、`Emm_BuildSpeed` 8B（`:70`）、`Emm_BuildStop` 5B（`:77`）、`Emm_BuildEnable` 6B（`:51`）、`Emm_BuildClear`（`:83`）；**带回复长度表** `reply_length()`（`:5-17`）与 `EmmParser_Feed()` 流式拼帧（`:21`）、`Emm_DecodePosition()`（`:88`） |
| `ghr/10.15/Drive/Include/stepper.h` + `stepper_inline.h` | 接口 + 内联实现 | 地址宏 `STEPPER_ADDR_Z 1 / TRAY 3 / X 4 / Y 5`（`ghr/10.15/Drive/Include/stepper.h:25-28`）；`3200` 脉冲/圈、`300RPM`、`acc=128`（`ghr/10.15/Drive/Include/stepper.h:38-40`）；接口 `Stepper_RunSpeedAt`(F6)、`Stepper_MoveRelativeAt`(FD raF=0)、`Stepper_MoveAbsoluteAt`(raF=1)、`Stepper_ZeroHereAt`(0A 6D)、`Stepper_ResetStallAt`(0E 52)、`Stepper_StopAll`（`ghr/10.15/Drive/Include/stepper.h:57-78`）；用**独立 TIM2 1ms 时基**，不占 SysTick（`ghr/10.15/Drive/Include/stepper.h:11`、`ghr/10.15/Drive/Include/stepper.h:84`） |
| 厂家 HAL 例程（被总结的那份） | DMA 收发 | 优点：DMA+IDLE 收帧；缺点见 §2.5 |

### 2.5 坑清单（写导读时必须带上）

1. **DMA 发送缓冲区是栈上局部数组** `uint8_t cmd[16]` → 函数返回后 DMA 可能还在读，数据被覆盖。建议静态/全局缓冲或阻塞发送（`EMM_V5_串口协议与工程指令总结.md:582-590`、`EMM_V5_串口协议与工程指令总结.md:731`）。
2. **接收不校验帧尾**、无 CRC：只解析地址+功能码，建议自己补长度与 `0x6B` 校验（`EMM_V5_串口协议与工程指令总结.md:592-600`、`EMM_V5_串口协议与工程指令总结.md:730`）。
3. **命令之间要留 10ms 间隔**，多机同步时尤其（`EMM_V5_串口协议与工程指令总结.md:602-610`）。
4. **忙等无超时** `while(rxFrameFlag==false)` → 电机不应答就死等（`EMM_V5_串口协议与工程指令总结.md:610-618`）。本项目全程 `EMM_TX_ONLY`，用"发完固定 wait_ms"替代反馈，代价是**等待时间≠真到位**（`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:48` 注释：发 40 圈后等 2500ms，不读到位反馈）。
5. **FD 绝不能像 F6 那样周期重发**：F6 是持续速度指令，重发无害；FD 重发会**叠加圈数**（`xrw/例36 Z轴单独上升/HARDWARE/EMM/motion_sequence.c:16` 注释）。
6. `FE` 只停车，不解除堵转保护；上电先 `0E 52` 是例 35/36 的固定套路（`xrw/例36 Z轴单独上升/HARDWARE/EMM/motion_sequence.c:34` 的 `stop→enable` 顺序）。

---

## 3. 两个参考工程：例 36 与例 35

两个工程结构相同：`CORE/ FWLIB/ HARDWARE/{EMM,LED,S20} SYSTEM/ USER/{PWM.uvprojx, main_xyz_grab.c} OBJ/ tests/{host,out}/`。注意主文件叫 `xrw/例35 视觉XY接近跟随(1)(2)/USER/main_xyz_grab.c`（不是 `main.c`），Keil 工程叫 `PWM.uvprojx`。

### 3.1 例 36：`xrw/例36 Z轴单独上升` —— 按键 + Z 轴最小闭环

- **做了什么**：上电 `FE 停车 → F3 使能 → 等 500ms → 等 PA0 有效按下 → FD 上升 20 圈 → 等 7s → 顶部稳定 500ms → FD 下降 20 圈 → 等 7s → DONE`。上电**不自转**，长按只触发一次、必须松手再按（`xrw/例36 Z轴单独上升/README.md:2-6`）。
- **只控地址 01 的 Z 轴**，不控 X/Y、不初始化舵机、不用视觉（`xrw/例36 Z轴单独上升/README.md:7`）。
- 参数集中在 `xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h`：`Z_SPEED_RPM 300`、`Z_ACCEL 128`、`Z_PULSES_PER_REV 3200`、`Z_TRAVEL_TURNS 20`、`Z_POSITION_WAIT_MS 7000`、`Z_UP_DIRECTION 1`（`xrw/例36 Z轴单独上升/README.md:22-27`）。
- 下发帧：上升 `01 FD 01 01 2C 80 00 00 FA 00 00 00 6B`、下降 `01 FD 00 01 2C 80 00 00 FA 00 00 00 6B`（`xrw/例36 Z轴单独上升/README.md:30`）。
- 按键写法（**全项目的"标准答案"**）：`GPIO_Mode_IN` + `GPIO_PuPd_DOWN`（内部下拉），`GPIO_ReadInputDataBit(GPIOA,GPIO_Pin_0)` 读到 **1 = 按下**（`xrw/例36 Z轴单独上升/USER/main_xyz_grab.c:19-21`、`xrw/例36 Z轴单独上升/USER/main_xyz_grab.c:25`）。
- 状态机在 `xrw/例36 Z轴单独上升/HARDWARE/EMM/motion_sequence.c`：`SEQUENCE_WAIT/Z_UP/GRAB/Z_DOWN/DONE/FAULT/ABORT`，20ms 消抖（`xrw/例36 Z轴单独上升/HARDWARE/EMM/motion_sequence.c:54`）、`release_seen` 保证松手才触发（`xrw/例36 Z轴单独上升/HARDWARE/EMM/motion_sequence.c:78-83`）、每段用 `wait_ms` 计时而不是等反馈（`xrw/例36 Z轴单独上升/HARDWARE/EMM/motion_sequence.c:59`、`xrw/例36 Z轴单独上升/HARDWARE/EMM/motion_sequence.c:74`）。
- 固件/目标名：`USER/PWM.uvprojx`、目标 `Z_ONLY_UP`、`OBJ/Z_ONLY_UP.hex`（`xrw/例36 Z轴单独上升/README.md:34-38`）。
- 另有 PC 侧单元测试目录 `tests/host`（如 `test_sequence.c`、`test_button_down_up.c`、`test_load_up.c`），`run_tests.ps1` 一键跑 → **作者有主机端仿真测试习惯**，这点值得在主工程沿用。

### 3.2 例 35：`xrw/例35 视觉XY接近跟随(1)(2)` —— 最完整的视觉抓取流程

- **做了什么**：USART1 收上位机视觉坐标 → PID?否，**比例式"接近跟随"**：X/Y 偏差 >8px 就按速度模式转，±8px 内发 FE 停车，600ms 无新坐标自动停 → XY 到位后 Z 下降 40 圈 → 舵机 180° 夹紧等 3s → 上升 40 圈 → XY 按"速度×时间"开环回起点 → 托盘按编号转位 → 下降 40 圈 → 松爪 0° → 上升 40 圈 → X 负向短移 4.5s 离开货物 → 发 `DONE,编号`（`xrw/例35 视觉XY接近跟随(1)(2)/README.md:8-10`、`xrw/例35 视觉XY接近跟随(1)(2)/README.md:32-34`）。
- **视觉协议**：`1,120,-30,45.6` = `目标编号, X像素偏差, Y像素偏差, Z深度`；视觉每 100ms 发一次，STM32 在 XY 追踪阶段回 `ACK,1`（`xrw/例35 视觉XY接近跟随(1)(2)/README.md:14-27`）。
- **控制阈值**：X 偏差 >+8 → X04 dir00 34RPM；Y 偏差 >+8 → Y05 dir00 150RPM；±8 内 FE 停车；**只用 F6 速度模式 + FE 停车，不用 FF 同步**（`xrw/例35 视觉XY接近跟随(1)(2)/README.md:39-42`）。
- **托盘 6 工位映射**：编号 1~6 → 0/60/120/180/240/300° → 脉冲 0/533/1067/1600/2133/2667；用 **FD 绝对位置**（`raF=1`），`TRAY_ADDRESS 3`、`TRAY_SPEED_RPM 30`、`TRAY_POSITION_WAIT_MS 3000`；上电只对 03 发一次 `03 0A 6D 6B` 清零，**必须人工先把 1 号位对准**（`xrw/例35 视觉XY接近跟随(1)(2)/README.md:69-90`、`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:15-20`）。
- **参数总表** `xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h`：地址 `X=4 Y=5 Z=1 TRAY=3`（`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:9-12`）、`X_SPEED_RPM 34`/`Y_SPEED_RPM 150`（`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:25`、`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:28`）、`VISION_TOLERANCE_PX 8`（`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:33`）、`VISION_TIMEOUT_MS 600`（`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:34`）、`Z_SPEED_RPM 2300`/`Z_TRAVEL_TURNS 40`/`Z_ACCEL 0`/`Z_POSITION_WAIT_MS 2500`（`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:43-52`）、`CLAW_OPEN_DEG 0`/`CLAW_GRAB_DEG 180`（`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:53`、`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:55`）、回程 `RETURN_*`（`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:59-65`）、短移 `X_CLEARANCE_MOVE_MS 4500`（`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:70`）。**文件末尾 20 多条 `#error` 编译期自检**（`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:74-110`），越界参数直接编不过——这是很好的工程习惯。
- **编码陷阱**：`xrw/例35 视觉XY接近跟随(1)(2)/USER/main_xyz_grab.c` 其实是"PA0 按键诊断"小程序（6 个引脚上拉、按下为低、LED2 闪编号，`xrw/例35 视觉XY接近跟随(1)(2)/USER/main_xyz_grab.c:12-16`、`xrw/例35 视觉XY接近跟随(1)(2)/USER/main_xyz_grab.c:47`），**真正的视觉抓取流程在 `xrw/例36 Z轴单独上升/HARDWARE/EMM/motion_sequence.c`(16431B) 与 `xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/emm_port.c`(7802B)**。读代码时不要被主文件误导。
- **串口分工**：`USART1`(PA9/PA10) 专用于视觉坐标与 ACK/printf；`USART2`(PA2) 只用于电机 TX 且**不开接收中断**（`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/emm_port.c:66-70`、`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/emm_port.c:49`、`xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/emm_port.c:53`；`USART1_IRQHandler` 在 `:203`）。
- 工程/目标名：`USER/PWM.uvprojx`、目标 `VISION_XY_FOLLOW`、`OBJ/VISION_XY_FOLLOW.hex`（`xrw/例35 视觉XY接近跟随(1)(2)/README.md:137-139`）。
- PC 侧测试更全：`tests/host/test_z_position_grab.c`(23166B)、`test_sequence.c`(8055B)、`s20_servo.h`。

### 3.3 与主工程 `ghr/10.15` 的继承关系

| 主工程里的东西 | 来源 | 证据 |
|---|---|---|
| PA0 内部下拉、"按下=高" | **例 36** | `README.md:48`、`ghr/10.15/新手联合开发完整教程.md:679` |
| 上电 `0E 52 解堵转 → FE 停车 → F3 使能 → 等 500ms` | **例 36** | `xrw/例36 Z轴单独上升/HARDWARE/EMM/motion_sequence.c:34-35` 与 `ghr/10.15/MY/main.c` 上电序列一致 |
| `FD` 相对位置只发一次、用固定 `wait_ms` 等（不读到反馈） | **例 36** | `xrw/例36 Z轴单独上升/HARDWARE/EMM/motion_sequence.c:16` 注释 |
| 电机地址 Z1/托3/X4/Y5 | **例 35** | `ghr/10.15/Drive/Include/stepper.h:25-28`、`ghr/10.15/Drive/Include/stepper.h:24` 注释"与例35/例36 的分配一致" |
| `F6` 速度模式 + `FE` 停车（不用 FF 同步） | **例 35** | `xrw/例35 视觉XY接近跟随(1)(2)/README.md:42` |
| 只用 PA2 发、PA3 不接（`EMM_TX_ONLY`） | **例 35** | `xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:6` |
| 独立 TIM2 1ms 时基（不占 SysTick） | **例 35** | `xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/emm_port.c` 的 TIM2 配置、主工程 `ghr/10.15/Drive/Include/stepper.h:11` |
| 参数集中宏 + 编译期 `#error` 自检 | **例 35** | `xrw/例35 视觉XY接近跟随(1)(2)/HARDWARE/EMM/app_config.h:74-110` |
| 视觉坐标解析 / 舵机动作序列 / 托盘 / 双相机 | **未继承（主工程暂无）** | 主工程 `MY/main.c` 只有 Z 轴地址宏 `MOTOR_ADDR 1`、PA0、上位机协议 |

**主工程相对例 35/36 的新增**：上位机协议与 USB CDC（`ghr/10.15/上位机/`、`USB/`）、`host_transport.h`/`host_protocol_inline.h`/`control_inline.h` 三件套、参数回读 `readback_mask`、锁存停车 `control_latch_stop`/`control_clear_stop`。**砍掉的**：视觉、舵机抓夹时序、托盘转位、X/Y 轴联动——所以"机械臂抓取"这条主线目前其实是**半成品**，导读里要如实说明。

### 3.4 `backup_before_*` 备份目录（只看不读）

- 例 35：**44 个**；例 36：**32 个**，合计 76 个；`xrw` 递归目录共 113 个（即 ~37 个是非备份目录）。
- 命名规律：`backup_before_<实验标签>_<YYYYMMDD>_<HHMMSS>`，例：`backup_before_boot_x_20261011_220741`、`backup_before_tray03_20261012_160402`、`backup_before_z2300_`、`backup_before_x_clearance4s_`、`backup_before_servo270_`、`backup_before_button20cycle_20261011_202119`、`backup_before_load_version_`。
- 价值：标签本身就是**调试编年史**（boot_x → tray02/03 → z2300 → x_clearance4s → servo270），能反推作者踩坑顺序；但被 `.gitignore:50` 的 `**/backup_before_*/` 排除，**不入库**，只是本地快照。
- 结论：**导读里只需一句"xrw 下有 76 个本地备份快照，是调试历史，不属于工程内容"**，不要逐个读，也不要统计代码量。

---

## 4. 编译日志（`build/uv4_*.log`，4 份）

全部使用 `*** Using Compiler 'V5.06 update 6 (build 750)'`，目录 `D:\study\keil\ARM\ARMCC\Bin`，`Build target 'STM32F407'`。

| 日志 | 类型 | 结果 | Program Size | 输出 |
|---|---|---|---|---|
| `build/uv4_10_15.log` | 10.15 主固件 Build | ❌ **失败**，13 条 `error - cannot create command input file '.\objects\xxx.__i'`（main/it/led/delay/startup/misc/rcc/gpio/usart/stm32f4xx_it…）+ `Target not created.` | — | — |
| `build/uv4_build3.log` | 屏工程 Build | ✅ 0 Error, 0 Warning | Code=2718 / RO=490 / RW=24 / ZI=1024 | `.\Objects\STM32F429.axf` |
| `build/uv4_final.log` | 屏工程 Rebuild（12 文件全编） | ✅ 0 Error, 0 Warning | 同上 | `.\Objects\STM32F407.axf` |
| `build/uv4_rebuild.log` | 屏工程 Rebuild | ✅ 0 Error, 0 Warning | 同上 | `.\Objects\STM32F429.axf` |

要点：
1. **三份成功的日志源文件列表里有 `lcd_init.c`/`lcd.c`**，即 **10.17/10.19 的 ST7735S 屏工程**，不是主固件。10.19 说明书里的数字与之逐字吻合：`Code=2718, RO=490, RW=24, ZI=1024`、`0 Error 0 Warning`（`ghr/10.19/10.19使用说明_ReadMe.txt:114-118`）。
2. **主固件 `ghr/10.15` 在仓库里没有任何"编译通过"的证据**：唯一一份它的日志失败在 `cannot create command input file '.\objects\main.__i'` → 典型原因是**输出目录 `Objects/` 不存在或大小写不一致**（`.gitignore:31` 排除了 `**/Objects/`，所以新克隆必然没有该目录，Keil 首次 Build 时需要自己创建/由 Rebuild 生成）。`环境复刻流程.md:68` 要求"F7 Rebuild 应为 0 Error 0 Warning"，这是**期望值而非已验证结论**。
3. **目标名 / 输出文件名不一致是老坑**：目标名 `STM32F407`，输出 `STM32F429.axf`/`STM32F429.hex`（`README.md:61` 明确写"历史遗留，别改"）。所以看到 `STM32F429.axf` 不必惊讶；也正因如此，**从产物文件名判断"这是哪个工程"不可靠**，要靠源文件列表。
4. `build/` 整个目录被 `.gitignore:108` 排除（`.gitignore:107` 注释：build/ 只是 ghr/10.19 的副本 + Keil 编译日志）——它属于"本地构建中转"，不是主工程的构建输出。**导读里应写成"build/ 是 10.19 屏工程的编译产物 + 4 份日志副本"**。

---

## 5. `ghr/10.17` 与 `ghr/10.19`：ST7735S 屏测试工程（各一句话定位）

**定位：两个独立实验工程，不参与主固件编译，各自打开自己的 `Project/STM32F407.uvprojx`；对主工程的唯一价值是"给分拣赛项那块强制显示屏提供现成驱动"。**

| 维度 | `ghr/10.17` | `ghr/10.19` |
|---|---|---|
| 一句话 | 从 10.15 派生的**最小点屏验证**：软件 SPI + 手写 `tft.c`，只跑屏幕测试，不初始化电机/舵机/USB | 从 10.15 派生的**完整液晶显示实验**：厂家 `lcd_init.c/lcd.c/lcdfont.h`，跑纯色/彩条/灰度/渐变/彩虹/方格 + 显示自家图片 |
| 文档 | `ghr/10.17/README.md`（62 行） | `ghr/10.19/10.19使用说明_ReadMe.txt`（150 行，GBK） |
| 驱动文件 | `Drive/Source/tft.c`、`tft_init.inc`、`Drive/Include/tft.h`、`tft_font.h`（8×16 ASCII）（`ghr/10.17/README.md:46-49`） | `Drive/Source/lcd_init.c`（底层 SPI/初始化）、`lcd.c`（画点线框/字符/汉字/图片）、`Drive/Include/lcdfont.h`（12/16/24/32 汉字 + 6×12/8×16 ASCII）（`ghr/10.19/10.19使用说明_ReadMe.txt:14-21`） |
| 屏 | 1.8 寸 ST7735S 128×160 | 1.8/1.77 寸 ST7735S 128×160（14PIN 或 8PIN） |
| 编译 | ARMCC V5.06 update 6，`0 Error 0 Warning`，产物 `Objects/TFT_ST7735_Test.hex`，**未下载实物验证**（`ghr/10.17/README.md:12`、`ghr/10.17/README.md:10`） | 同编译器，`0 Error 0 Warning`，`STM32F407.hex`（约 9.1KB）+ `STM32F407.axf`（`ghr/10.19/10.19使用说明_ReadMe.txt:54-55`、`ghr/10.19/10.19使用说明_ReadMe.txt:116`） |
| 与主工程关系 | 只沿用启动文件/时钟/标准库/SysTick 延时/PC13 LED；**10.15 原工程未改**（`ghr/10.17/README.md:3`） | 以 10.15 为基础加 LCD 驱动，工程结构完全一致（`ghr/10.19/10.19使用说明_ReadMe.txt:7`、`ghr/10.19/10.19使用说明_ReadMe.txt:52-53`） |
| 额外内容 | 诊断变量 `g_tft_stage` 8 个阶段、0x01 软复位（`ghr/10.17/README.md:39`） | `MY/pic.h` 258936B（`const unsigned char gImage_pic[40960]`，128×160×2B 只占 Flash）+ `tools/` 图片工具 |

**⚠ 一处必须核实的矛盾（写进导读的"待确认"）**：两个工程对 P2 排针的 **RES 与 DC 分配正好互换**——
- 10.17：`2 PC5/RESET`、`3 PB14/DC`（`ghr/10.17/README.md:20-27`），并提醒两个排针 1~8 不能直插，必须按表交叉（`ghr/10.17/README.md:29`）。
- 10.19：`6 PB14/RES`、`7 PC5/DC`（`ghr/10.19/10.19使用说明_ReadMe.txt:39-40`），且称"直接插屏"（`:27`）。
→ 到底哪份对，**必须以实物丝印/原理图为准**；若 10.19 的驱动在实屏上白屏而 10.17 能亮（或反之），优先怀疑这一对引脚。

**`build/10.19/tools` 与 `ghr/10.19/tools` 是什么**：同一套图片转换工具（两份内容相同）——`img2c.py`(3911B，图片→C 数组，等比缩放+居中裁剪到 128×160)、`make_demo_pic.py`(5107B，生成示例日落图)、`demo_preview.png`(8070B，PC 上双击预览)；用法 `python tools\img2c.py 你的图.jpg MY\pic.h`（`ghr/10.19/10.19使用说明_ReadMe.txt:122-141`）。**它们只服务屏工程，与主固件无关**；但对分拣赛项"显示屏显示货物图片"这一条是现成工具链。

---

## 6. 工程协作规范（三条铁律 + 约定）

两种文档：`ghr/10.15/新手联合开发完整教程.md`（1254 行，从注册 GitHub 讲起）与 `ghr/10.15/CONTRIBUTING.md`（319 行，精简版）。仓库根 `README.md:37-41` 也摘了三条。

### 6.1 三条铁律

**铁律一：`main.c` 里的 `*_inline.h` 是实现代码，不能删**（`ghr/10.15/新手联合开发完整教程.md:535`）
- 原因：Keil 保存工程时会**用内存里的旧文件列表覆盖 `Project/STM32F407.uvprojx`**；从外部加的 `.c` 可能不参与编译，链接报 `Error: L6218E: Undefined symbol Gripper_Grab`（`:560-565`、`ghr/10.15/CONTRIBUTING.md:196-200`）。
- 做法：模块实现放 `Drive/Include/*_inline.h`，由 `main.c` `#include` 带入（`:567-568`）。
- 后果：`Drive/Source/gripper.c`、`stepper.c` 是**参考副本、不参与编译**；两份实现要手动同步（`:574-576`）。
- 禁止：把 `.c` 加进工程（→ `Multiply defined symbol`）、删 `*_inline.h`、只改一半就提交（`:580-585`）。要正式拆分必须"Keil 加文件 + 删 include + 补 `stepper.c` 的 `#include "servo.h"`"**一次做完**（`:587-595`）。

**铁律二：不要在没沟通的情况下改 Keil 工程文件**（`ghr/10.15/新手联合开发完整教程.md:597`）
- `Project/STM32F407.uvprojx` 入库；`.uvoptx`（调试器/断点/窗口）不入库（`:601-604`）。
- 一边开 Keil 一边在外部改 `.uvprojx` → 会被 Keil 覆盖；加文件必须走 Keil 界面并**立刻提交**（`:606-615`）。

**铁律三：源文件混合编码，编辑时保持原样**（`ghr/10.15/新手联合开发完整教程.md:617`）
- GBK：`Drive/Source/delay.c`、`led.c`、`Drive/Include/delay.h`、`led.h`、`MY/stm32f4xx_it.c`；UTF-8：`MY/main.c`、`servo*`、`gripper*`、`key*`、`stepper*`（`:621-624`）。
- ARMCC 两种都能编译，**不要批量转码**（`:626`）；提交前 `git diff --stat`，若"只改了几行却显示几百行"= 编码被改，`git checkout --` 撤销（`:632-642`）。新增文件统一 UTF-8（`:644`）。

### 6.2 引脚 / 地址 / 电源约定（写代码前必须对齐）

| 功能 | 引脚 | 约定 |
|---|---|---|
| 舵机（夹爪） | **PB6 / TIM4_CH1** | 50Hz，500~2500us ↔ 0~270°，上电停在松开位（`README.md:47`、`ghr/10.15/新手联合开发完整教程.md:678`） |
| 按键 | **PA0** | **内部下拉，按下读到高**；别按例 35 的上拉写法改（`README.md:48`、教程 `:679`） |
| 步进（EMM V5） | **PA2 / USART2_TX** | 115200 8N1，只发不收，PA3 留空（`README.md:49`、教程 `:680`） |
| 板载 LED | PC13 | 低电平点亮（`README.md:50`） |
| 调试串口 | PA9/PA10 | USART1 预留（教程 `:682`） |
| 电机地址 | — | **Z=1、托盘=3、X=4、Y=5**（`README.md:49`、教程 `:684-693`、`ghr/10.15/Drive/Include/stepper.h:25-28`） |
| 电源 | — | 驱动器独立 12~48V、舵机 4.8~7.4V，**必须共地**（`README.md:52`、教程 `:695-699`） |

### 6.3 新增模块规范（教程 §6.4 / `ghr/10.15/CONTRIBUTING.md:251-267`）

四文件结构：`Drive/Include/xxx.h`（接口+参数宏）、`Drive/Include/xxx_inline.h`（实际参与编译的实现）、`Drive/Source/xxx.c`（参考副本，不参与编译）、`MY/main.c`（只调用接口）。要求：参数宏集中在头文件顶部；注释写清**占用哪个外设/为什么这么写/有哪些坑**；提交前单独编译 `0 error 0 warning`（`ghr/10.15/新手联合开发完整教程.md:646-672`）。

### 6.4 提交前自检清单（`ghr/10.15/CONTRIBUTING.md:296-304`）

- Rebuild `0 Error 0 Warning`；下载到板子**实测**过；
- `git status` 里没有 `Objects/`、`*.hex`、`*.axf`（`.gitignore` 已挡，`.gitignore:8-37`）；
- 不提交 `*_inline.h` 的重复实现、私钥（`.gitignore:64-71`）、`backup_before_*/`（`.gitignore:49-51`）、`*.log`（`.gitignore:55`）；
- `git diff` 里没有"整个文件变红"（编码被改）；
- 动了 `.uvprojx` 或 `MY/main.c` 要在群里说（`ghr/10.15/CONTRIBUTING.md:304`）。

### 6.5 环境复刻要点（`环境复刻流程.md`，251 行）

- 四层可独立复刻：**L1 固件**（Keil+ST-Link，0 额外空间）/ **L2 上位机**（Python tkinter，~50MB，可 `--sim`）/ **L3 视觉**（`new_eye` 双相机 + SAM，~8GB）/ **L4 大模型**（`Qwen` 本地千问，~20GB）（`环境复刻流程.md:13-18`）。
- 建议克隆到**同一路径** `D:\study\project\robot-competition`，因为多处文档与 `new_eye/config.json` 写死了绝对路径（`环境复刻流程.md:5`）。
- 克隆后 `git log --oneline -1` 应看到 `b89d8e1`（`环境复刻流程.md:34`）；L1 的上电现象：LED 闪 1/2/3 次报串口自检 → 舵机回松开位 180° → 按 PA0 抓夹一步 45° 且全电机速度模式转 1 秒（`:73`）。
- 明确写着"`ghr/10.17`、`ghr/10.19` 都是独立工程，各自打开自己的 `Project/STM32F407.uvprojx`"（`:83`）。

---

## 7. 待确认 / 建议 Lead 在导读里标注的风险

1. **主固件是否有"编译通过"的实测结论**：仓库里没有 10.15 的成功日志（§4），`build/uv4_10_15.log` 失败点是 `Objects/` 目录缺失。建议 Lead 让有 Keil 环境的人实跑一次 Rebuild 并留存日志。
2. **10.17 / 10.19 的 RES/DC 引脚互换**（§5），需实物核对；这决定"分拣显示屏能不能直接复现"。
3. **整机外形 ≤350×350×450mm**（`newpdftext.txt:270`）：需实测包络，尤其相机支架与 Z 轴 40 圈行程。
4. **分拣的"一件一显示 + 15 秒节奏"** 与主工程"PA0 多按 + 固定等 2.5s"的架构差距（§1.4、§3.3），这是功能缺口而不只是参数问题。
5. **机械臂主线是半成品**：主工程目前只有 Z 轴（`MOTOR_ADDR 1`）+ 舵机 + 上位机协议，X/Y/托盘逻辑仍只存在于 `xrw/例35`（§3.3）。
6. **`tray` 清零语义**：`0A 6D` 只清零计数（`EMM_V5_串口协议与工程指令总结.md:76`），例 35 也强调"这不是机械回零、必须人工对准 1 号位"（`xrw/例35 视觉XY接近跟随(1)(2)/README.md:89-90`）。任何人接手都要先做这个机械对准动作。

---

## 8. 一句话给《全项目导读》的素材概括

> `xrw/例36` 提供"按键 + 单轴 FD 升降"的最小可靠闭环，`xrw/例35` 提供"视觉接近跟随 + Z 取放 + 六工位托盘 + 开环回程"的完整流程，主工程 `ghr/10.15` 把两者的按键/上电/协议/地址约定抽成 4 个 `*_inline.h` 模块并接上上位机协议，但**尚未接入视觉与舵机时序**；赛题对应**智能分拣**（托盘 + 六储物盒 + AI 识别 + 强制显示屏 + 15 秒节奏，外形 ≤350×350×450）；协议为 EMM V5 的 `[addr][fn][…][0x6B]` 大端帧，走 PA2/USART2 只发；仓库内唯一确认的编译成功记录属于 10.17/10.19 两个 ST7735S 点屏实验工程（0E/0W，Code 2718B），主固件编译结果待补。
