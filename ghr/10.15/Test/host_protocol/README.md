# 上位机行协议：主机侧自测

这里放的是**固件通信层**的验证代码。它直接在 PC 上编译运行真实的固件源码
（`Drive/Include/host_protocol_inline.h`、`Drive/Include/control_inline.h`），
只把两类东西换成桩：

| 换掉的东西 | 桩件位置 | 说明 |
| --- | --- | --- |
| 硬件调用（`Stepper_*` / `Servo_SetAngle` / 毫秒时基） | `test_host_protocol.c` 顶部 `HostTest_*` | 记录调用次数与参数，不碰寄存器 |
| USB 传输层（`HostLink_*`） | 同文件 `HostLink_*` | 用一个内存收发队列冒充 CDC |

这样在**没有板子、也没有 USB 协议栈**的情况下，协议本身就能被逐字节验证；
等 USB 栈接进来时，协议层已经被证明是对的。

## 跑一次

```cmd
cd ghr\10.15\Test\host_protocol
run_host_tests.cmd
```

脚本做四件事：

1. 用 `zig cc` 编译（`python -m ziglang cc`，因为本机没有 MSVC/gcc）；
2. 跑 C 侧协议测试，并把固件真实发出的**每一帧**写进 `frames.txt`；
3. 用上位机自己的 `ghr/10.15/上位机/protocol.py` 把 `frames.txt` 逐帧再解析一遍
   （跨实现交叉验证：C 侧编出来的帧，Python 侧必须认得）；
4. 用真实的 **ARMCC V5**（`D:\study\keil\ARM\ARMCC\Bin\armcc.exe`，与 Keil 工程同一个编译器）
   把同一个翻译单元再编一遍，确认固件工具链也能零警告通过。

## 覆盖的用例

| 用例 | 验证内容 |
| --- | --- |
| `hello` | 握手前不 ready、握手后 ready、协议版本不符回 `E02` |
| `hello-rehandshake` | 复用同一个请求 id 重新握手也要成功（旧会话的缓存不能把它挡成 `E12`，否则界面永远连不回来） |
| `ping/status` | 毫秒时基、锁存位、控制权、运行轴/队列计数如实上报 |
| `doc-example` | 方案文档里逐字节验证过的 `$12,motor_speed,1,0,60,1*4A` 能被接受，且下发的 rpm/acc/方向正确 |
| `dedupe` | 同 id 同内容只重发应答、不重复执行；同 id 不同内容回 `E12` |
| `errors` | 坏校验 `E07`、未知命令 `E01`、非法轴 `E03`、参数缺失 `E02(argc)`、超速 `E02(speed_limit)`，且坏帧不污染会话缓存 |
| `position` | 位置指令**非阻塞**（`wait_ms=0`）、无位置基准回 `E13`、运动中清零回 `E09`、无回读查询回 `E11` |
| `enable` | 使能/取消使能下发内容与非法轴 |
| `latch` | `stop_all` 后回 `E10`、按 `frame_gap_ms` 补发四轴停止、`stop_clear` 恢复 |
| `stop-char` | 传输层锁存的 `!` 与字节流里的 `!` 都必须置锁存 |
| `framing` | 残帧被下一个 `$` 丢弃、超长帧回 `E06` 且计数 +1、96 字节上限边界 |
| `params` | `param_get/param_set` 的未知参数、越界、非法值 |
| `gripper` | 夹紧 135°（=180-45，**不是 0°**）、松开 180°、单步 45° 方向不反转；调试角度越界回 `E02` |
| `gripper-init` | 上电初始化必须直接把 PB6 输出到松开位（180°），且不得伪造"等待结束"事件（曾经 `Servo_Init` 没人调用，夹爪一动不动） |
| `telemetry` | 订阅周期生效、未到周期不发；夹爪等待结束只报 `settle_elapsed`，**不声称到位** |
| `link-timeout` | 3 秒无上位机字节即报事件并锁存停车，但**不关使能**（防止 Z 轴下滑） |
| `split` | 一帧拆三段、两帧粘一包、帧外垃圾字节全部正确处理 |
| `readback` | PA3 回读：先发 3 字节询问帧、等回包才回 `ok`、`0x36/0x35/0x3A` 解码、负数、超时回 `E04` 且不编数据、同 id 重发不重复问驱动器、未接回读的轴 `E11` |
| `multiaxis` | `readback_mask` 决定哪些轴接了回读线：**默认 15 = 四轴都接**、`hello` 报 `readback=1/3/4/5`；改成 1 之后 3/4/5 号轴回 `E11`；非订阅轴也能单独查；遥测按 `1→3→4→5` 轮询且各轴读数不串台、轮完回到 1 号轴 |

## 两条必须守住的规矩

1. **`.c` 与 `.cmd` 里不要出现非 ASCII 字符。**
   ARMCC V5 按本机区域设置（GBK）读源码，UTF-8 的中文字符串字面量会报
   `missing closing quote`；cmd.exe 读 `.cmd` 同理，会把一行截成几段乱码命令。
   中文**注释**没问题（与工程里既有的 UTF-8 头文件一致），中文只放在 `.md` 里。
2. 这里验证的是**协议行为**，不是硬件。真正的 USB 枚举、跨包、拔插重连、以及
   驱动器回读解析，必须等 `host_usb.c` 接进来并上板才能算数。
