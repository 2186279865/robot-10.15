# EMM V5 串口协议与工程指令总结

> 本文根据当前文件夹中的 6 个 STM32F407 工程源码整理。
> 6 个工程的 `Emm_V5.c` 内容完全一致，使用同一套 EMM V5 步进闭环驱动串口协议。

## 一、工程范围

当前文件夹包含以下工程：

- `STM32F407_串口通讯__速度模式`
- `STM32F407_串口通讯__位置模式`
- `STM32F407_串口通讯__读取电机实时位置`
- `STM32F407_串口通讯__读取电机实时转速`
- `STM32F407_串口通讯__多机同步控制`
- `STM32F407_串口通讯__发送命令触发回零`

协议核心文件位于每个工程的：

- `Src/Emm_V5.c`
- `Inc/Emm_V5.h`

串口接收和 DMA 配置位于：

- `Src/usart.c`
- `Inc/usart.h`
- `Src/stm32f4xx_it.c`

## 二、串口配置

| 项目 | 配置 |
|---|---|
| 串口 | USART1 |
| TX | PA9 |
| RX | PA10 |
| 波特率 | 115200 |
| 数据位 | 8 bit |
| 停止位 | 1 bit |
| 校验 | 无校验 |
| 流控 | 无硬件流控 |
| 通信格式 | 115200 8N1 |
| 发送方式 | DMA 普通模式 |
| 接收方式 | DMA 循环模式 |
| 接收缓冲区 | 255 字节 |
| 一帧结束判断 | UART IDLE 中断 |
| 固定帧尾 | `0x6B` |

## 三、通用帧格式

所有命令都以二进制字节发送，不是 ASCII 字符串。

```text
[地址] [功能码] [辅助码或参数...] [0x6B]
```

说明：

- `地址`：目标电机地址，工程中使用过 1、2、3、4；地址 0 用于广播同步。
- `功能码`：命令类型。
- `辅助码或参数`：由具体命令决定。
- `0x6B`：协议固定结束字节。源码中称为“校验字节”，但实际没有动态计算 CRC。
- 多字节参数均采用大端顺序，即高字节在前、低字节在后。

## 四、完整指令清单

### 1. 当前位置清零

函数：

```c
Emm_V5_Reset_CurPos_To_Zero(uint8_t addr);
```

发送帧：

```text
[addr] [0x0A] [0x6D] [0x6B]
```

长度：4 字节。

作用：将电机当前位置设置为零点。

---

### 2. 解除堵转保护

函数：

```c
Emm_V5_Reset_Clog_Pro(uint8_t addr);
```

发送帧：

```text
[addr] [0x0E] [0x52] [0x6B]
```

长度：4 字节。

作用：解除电机堵转保护状态。

---

### 3. 读取系统参数

函数：

```c
Emm_V5_Read_Sys_Params(uint8_t addr, SysParams_t s);
```

查询命令如下：

| 枚举 | 功能码 | 含义 | 发送帧 |
|---|---:|---|---|
| `S_VER` | `0x1F` | 固件版本和硬件版本 | `[addr] 1F 6B` |
| `S_RL` | `0x20` | 相电阻和相电感 | `[addr] 20 6B` |
| `S_PID` | `0x21` | PID 参数 | `[addr] 21 6B` |
| `S_VBUS` | `0x24` | 总线电压 | `[addr] 24 6B` |
| `S_CPHA` | `0x27` | 相电流 | `[addr] 27 6B` |
| `S_ENCL` | `0x31` | 线性化校准后的编码器值 | `[addr] 31 6B` |
| `S_TPOS` | `0x33` | 电机目标位置角度 | `[addr] 33 6B` |
| `S_VEL` | `0x35` | 电机实时转速 | `[addr] 35 6B` |
| `S_CPOS` | `0x36` | 电机实时位置角度 | `[addr] 36 6B` |
| `S_PERR` | `0x37` | 电机位置误差角度 | `[addr] 37 6B` |
| `S_FLAG` | `0x3A` | 使能、到位、堵转状态标志 | `[addr] 3A 6B` |
| `S_ORG` | `0x3B` | 回零中、回零失败状态标志 | `[addr] 3B 6B` |
| `S_Conf` | `0x42 0x6C` | 驱动参数 | `[addr] 42 6C 6B` |
| `S_State` | `0x43 0x7A` | 系统状态参数 | `[addr] 43 7A 6B` |

#### 3.1 读取实时转速返回帧

查询：

```text
[addr] [0x35] [0x6B]
```

工程按 6 字节返回帧解析：

```text
[addr] [0x35] [direction] [speed_H] [speed_L] [0x6B]
```

速度计算：

```c
speed = ((uint16_t)rxCmd[3] << 8) | rxCmd[4];
if (rxCmd[2] != 0) speed = -speed;
```

#### 3.2 读取实时位置返回帧

查询：

```text
[addr] [0x36] [0x6B]
```

工程按 8 字节返回帧解析：

```text
[addr] [0x36] [direction] [pos_31:24] [pos_23:16] [pos_15:8] [pos_7:0] [0x6B]
```

位置换算：

```c
position_raw = ((uint32_t)rxCmd[3] << 24)
             | ((uint32_t)rxCmd[4] << 16)
             | ((uint32_t)rxCmd[5] << 8)
             |  (uint32_t)rxCmd[6];
angle = position_raw * 360.0f / 65536.0f;
if (rxCmd[2] != 0) angle = -angle;
```

---

### 4. 修改开环/闭环控制模式

函数：

```c
Emm_V5_Modify_Ctrl_Mode(uint8_t addr, bool svF, uint8_t ctrl_mode);
```

发送帧：

```text
[addr] [0x46] [0x69] [svF] [ctrl_mode] [0x6B]
```

参数：

- `svF = 0`：不保存
- `svF = 1`：保存
- `ctrl_mode = 0`：关闭脉冲输入引脚
- `ctrl_mode = 1`：开环模式
- `ctrl_mode = 2`：闭环模式
- `ctrl_mode = 3`：En 端口复用为多圈限位开关输入，Dir 端口复用为到位输出高电平

长度：6 字节。

---

### 5. 电机使能控制

函数：

```c
Emm_V5_En_Control(uint8_t addr, bool state, bool snF);
```

发送帧：

```text
[addr] [0xF3] [0xAB] [state] [snF] [0x6B]
```

参数：

- `state = 0`：关闭使能
- `state = 1`：开启使能
- `snF = 0`：立即执行
- `snF = 1`：加入多机同步运动

长度：6 字节。

---

### 6. 速度模式控制

函数：

```c
Emm_V5_Vel_Control(uint8_t addr, uint8_t dir,
                   uint16_t vel, uint8_t acc, bool snF);
```

发送帧：

```text
[addr] [0xF6] [dir] [vel_H] [vel_L] [acc] [snF] [0x6B]
```

长度：8 字节。

参数：

- `dir = 0`：CW
- `dir != 0`：CCW
- `vel`：速度，单位 RPM，源码注释范围为 0～5000 RPM
- `acc`：加速度，范围 0～255
- `acc = 0`：直接启动
- `snF = 0`：立即运动
- `snF = 1`：等待同步触发

工程示例：

```c
Emm_V5_Vel_Control(1, 0, 1000, 10, 0);
```

发送字节：

```text
01 F6 00 03 E8 0A 00 6B
```

---

### 7. 位置模式控制

函数：

```c
Emm_V5_Pos_Control(uint8_t addr, uint8_t dir,
                   uint16_t vel, uint8_t acc,
                   uint32_t clk, bool raF, bool snF);
```

发送帧：

```text
[addr]
[0xFD]
[dir]
[vel_H] [vel_L]
[acc]
[clk_31:24] [clk_23:16] [clk_15:8] [clk_7:0]
[raF]
[snF]
[0x6B]
```

长度：13 字节。

参数：

- `dir = 0`：CW
- `dir != 0`：CCW
- `vel`：速度，单位 RPM
- `acc`：加速度；0 表示直接启动
- `clk`：脉冲数，32 位无符号数
- `raF = 0`：相对运动
- `raF = 1`：绝对位置运动
- `snF = 0`：立即运动
- `snF = 1`：等待同步触发

工程示例：

```c
Emm_V5_Pos_Control(1, 0, 1000, 0, 3200, 0, 0);
```

发送字节：

```text
01 FD 00 03 E8 00 00 00 0C 80 00 00 6B
```

其中 `3200 = 0x00000C80`。工程注释按 16 细分计算，3200 个脉冲约等于一圈。

位置运动完成时，工程注释中给出的 Reached 返回帧为：

```text
[addr] [0xFD] [0x9F] [0x6B]
```

---

### 8. 立即停止运动

函数：

```c
Emm_V5_Stop_Now(uint8_t addr, bool snF);
```

发送帧：

```text
[addr] [0xFE] [0x98] [snF] [0x6B]
```

长度：5 字节。

作用：立即停止电机运动。

---

### 9. 多机同步触发

函数：

```c
Emm_V5_Synchronous_motion(uint8_t addr);
```

发送帧：

```text
[addr] [0xFF] [0x66] [0x6B]
```

长度：4 字节。

多机同步工程使用地址 `0` 广播：

```text
01 FD ... snF=1 6B
02 FD ... snF=1 6B
03 FD ... snF=1 6B
04 FD ... snF=1 6B
00 FF 66 6B
```

发送后 1～4 号电机同时开始运动。

---

### 10. 设置挡圈回零零点

函数：

```c
Emm_V5_Origin_Set_O(uint8_t addr, bool svF);
```

发送帧：

```text
[addr] [0x93] [0x88] [svF] [0x6B]
```

长度：5 字节。

参数：

- `svF = 0`：不保存
- `svF = 1`：保存

作用：将当前位置设置为挡圈回零零点。

---

### 11. 修改回零参数

函数：

```c
Emm_V5_Origin_Modify_Params(
    uint8_t addr,
    bool svF,
    uint8_t o_mode,
    uint8_t o_dir,
    uint16_t o_vel,
    uint32_t o_tm,
    uint16_t sl_vel,
    uint16_t sl_ma,
    uint16_t sl_ms,
    bool potF
);
```

发送帧：

```text
[addr]
[0x4C]
[0xAE]
[svF]
[o_mode]
[o_dir]
[o_vel_H] [o_vel_L]
[o_tm_31:24] [o_tm_23:16] [o_tm_15:8] [o_tm_7:0]
[sl_vel_H] [sl_vel_L]
[sl_ma_H]  [sl_ma_L]
[sl_ms_H]  [sl_ms_L]
[potF]
[0x6B]
```

长度：20 字节。

参数：

- `svF = 0`：不保存；`svF = 1`：保存
- `o_mode = 0`：单圈就近回零
- `o_mode = 1`：单圈按方向回零
- `o_mode = 2`：多圈无限位碰撞回零
- `o_mode = 3`：多圈有限位开关回零
- `o_dir = 0`：CW；非零：CCW
- `o_vel`：回零速度，单位 RPM
- `o_tm`：回零超时时间
- `sl_vel`：无限位碰撞检测速度，单位 RPM
- `sl_ma`：无限位碰撞检测电流，单位 mA
- `sl_ms`：无限位碰撞检测时间，单位 ms
- `potF = 0`：上电不自动回零
- `potF = 1`：上电自动回零

---

### 12. 触发回零

函数：

```c
Emm_V5_Origin_Trigger_Return(uint8_t addr, uint8_t o_mode, bool snF);
```

发送帧：

```text
[addr] [0x9A] [o_mode] [snF] [0x6B]
```

长度：5 字节。

`o_mode` 取值：

- `0`：单圈就近回零
- `1`：单圈方向回零
- `2`：多圈无限位碰撞回零
- `3`：多圈有限位开关回零

`snF`：

- `0`：立即执行
- `1`：等待同步触发

工程示例：

```c
Emm_V5_Origin_Trigger_Return(1, 0, 0);
```

发送字节：

```text
01 9A 00 00 6B
```

---

### 13. 强制中断回零

函数：

```c
Emm_V5_Origin_Interrupt(uint8_t addr);
```

协议帧应为：

```text
[addr] [0x9C] [0x48] [0x6B]
```

协议长度：4 字节。

但是当前源码调用为：

```c
HAL_UART_Transmit_DMA(&huart1, (uint8_t *)cmd, 5);
```

由于 `cmd[4]` 没有显式赋值，实际可能发送：

```text
[addr] [0x9C] [0x48] [0x6B] [0x00]
```

建议将源码中的发送长度从 `5` 改为 `4`，并重新核对驱动器手册。

## 五、工程示例行为

| 工程 | 启动后实际执行 |
|---|---|
| 速度模式 | 地址 1，CW，1000 RPM，加速度 10 |
| 位置模式 | 地址 1，CW，1000 RPM，相对移动 3200 脉冲 |
| 读取实时位置 | 先相对运动 3200 脉冲，再查询 `S_CPOS` |
| 读取实时转速 | 先以 1000 RPM 运行，再查询 `S_VEL` |
| 多机同步控制 | 地址 1～4 发送同步位置命令，再广播同步触发 |
| 触发回零 | 设置当前位置零点，移动 800 脉冲，再触发回零 |

## 六、接收处理方式

工程初始化时：

```c
HAL_UART_Receive_DMA(&huart1, (uint8_t *)rxCmd, CMD_LEN);
```

其中：

```c
#define CMD_LEN 255
```

UART IDLE 中断发生后，代码执行：

```c
HAL_UART_DMAStop(&huart1);
rxCount = CMD_LEN - __HAL_DMA_GET_COUNTER(&hdma_usart1_rx);
rxFrameFlag = true;
HAL_UART_Receive_DMA(&huart1, (uint8_t *)rxCmd, CMD_LEN);
```

因此接收流程是：

1. DMA 持续接收。
2. 总线空闲触发 IDLE 中断。
3. 停止 DMA。
4. 计算本次接收到的字节数。
5. 置位 `rxFrameFlag`。
6. 重新开启 DMA 接收。

## 七、源码注意事项

### 1. DMA 发送缓冲区是局部变量

各个发送函数都在栈上定义：

```c
uint8_t cmd[16] = {0};
```

然后立即调用 DMA 异步发送。函数返回后局部数组生命周期结束，存在 DMA 尚未读取完毕时数据被覆盖的风险。建议改为静态或全局发送缓冲区，或改用阻塞发送并等待完成。

### 2. 接收代码没有统一校验帧尾

工程示例通常只判断：

- 地址
- 功能码
- 接收长度

没有统一检查最后一个字节是否为 `0x6B`，也没有做 CRC 校验。实际应用建议增加帧尾、地址、功能码和长度校验。

### 3. 多机同步发送间隔

多机工程在每个命令之间延时 10 ms，避免连续发送造成粘包或驱动器处理冲突：

```c
HAL_Delay(10);
```

### 4. 等待接收时使用忙等

示例使用：

```c
while (rxFrameFlag == false);
```

实际产品代码建议增加超时机制，避免驱动器掉线时程序永久阻塞。

## 八、EMM V5 串口协议副本

以下内容是从工程 `Emm_V5.h` 和 `Emm_V5.c` 汇总出的独立协议副本，可直接作为上位机、单片机或 Python 串口程序的接口参考。

### 协议常量

```text
波特率：115200
数据位：8
停止位：1
校验：无
帧尾：0x6B
字节序：大端
```

### 协议命令速查表

```text
当前位置清零：       [addr] 0A 6D 6B
解除堵转保护：       [addr] 0E 52 6B
读取固件版本：       [addr] 1F 6B
读取相电阻电感：     [addr] 20 6B
读取 PID：           [addr] 21 6B
读取总线电压：       [addr] 24 6B
读取相电流：         [addr] 27 6B
读取编码器值：       [addr] 31 6B
读取目标位置：       [addr] 33 6B
读取实时转速：       [addr] 35 6B
读取实时位置：       [addr] 36 6B
读取位置误差：       [addr] 37 6B
读取状态标志：       [addr] 3A 6B
读取回零状态：       [addr] 3B 6B
读取驱动参数：       [addr] 42 6C 6B
读取系统状态：       [addr] 43 7A 6B
修改控制模式：       [addr] 46 69 svF ctrl_mode 6B
修改回零参数：       [addr] 4C AE ... 6B
设置回零零点：       [addr] 93 88 svF 6B
触发回零：           [addr] 9A o_mode snF 6B
中断回零：           [addr] 9C 48 6B
电机使能：           [addr] F3 AB state snF 6B
速度控制：           [addr] F6 dir vel_H vel_L acc snF 6B
位置控制：           [addr] FD dir vel_H vel_L acc clk[4] raF snF 6B
立即停止：           [addr] FE 98 snF 6B
同步触发：           [addr] FF 66 6B
```

### 参数编码规则

```text
uint16_t：高字节、低字节
uint32_t：最高字节、次高字节、次低字节、最低字节
bool：0 或 1
```

示例：

```text
速度 1000 RPM = 0x03E8
发送为：03 E8

脉冲数 3200 = 0x00000C80
发送为：00 00 0C 80
```

### 常用完整示例帧

```text
地址 1 速度模式，CW，1000 RPM，加速度 10：
01 F6 00 03 E8 0A 00 6B

地址 1 位置模式，CW，1000 RPM，直接启动，3200 脉冲，相对运动：
01 FD 00 03 E8 00 00 00 0C 80 00 00 6B

地址 1 查询实时转速：
01 35 6B

地址 1 查询实时位置：
01 36 6B

地址 1 设置当前点为回零零点且不保存：
01 93 88 00 6B

地址 1 触发单圈就近回零，不同步：
01 9A 00 00 6B

地址 0 广播触发多机同步：
00 FF 66 6B

地址 1 立即停止，不同步：
01 FE 98 00 6B
```

### 常见返回帧

```text
位置到达（Reached）：
[addr] FD 9F 6B

实时转速返回：
[addr] 35 direction speed_H speed_L 6B

实时位置返回：
[addr] 36 direction pos_31:24 pos_23:16 pos_15:8 pos_7:0 6B
```

## 九、协议使用建议

1. 每次发送完整的一帧，不要把多条命令拼在一起发送。
2. 连续控制多个驱动器时，命令之间建议保留约 10 ms 间隔。
3. 需要同步运动时，先对各个电机发送 `snF=1` 的运动命令，再发送地址 0 的同步触发命令。
4. 读取返回值时至少检查地址、功能码、长度和帧尾 `0x6B`。
5. 发送函数使用 DMA 时，不要使用函数返回后立即失效的局部缓冲区。
6. `Emm_V5_Origin_Interrupt()` 当前工程存在发送长度为 5 字节的问题，应按协议核对并修正。
