# 上位机（PC 端）

按 `../上位机方案-v3-TypeC-CDC.md` 实现的机器人抓取系统上位机：

* 通信走 **板载 Type-C → STM32 USB OTG FS Device → CDC 虚拟串口**（不是 USB 转 TTL 接 USART1）。
* 界面是 tkinter 分页：四轴控制、舵机夹爪、参数、动作序列、原始日志。
* 没有硬件时用**模拟后端**跑同一套协议，界面与真实串口完全同路。

## 当前状态（重要，别误判）

| 部分 | 状态 |
| --- | --- |
| PC 端协议层、串口链路、统一客户端、模拟器 | 已完成，68 个自动化测试全绿 |
| PC 端界面（四轴 / 夹爪 / 参数 / 序列 / 日志） | 已完成，可在模拟模式下完整操作 |
| 固件侧 USB CDC 通信层 | **尚未实现**（方案第 8 节步骤 0~5 还没开工） |
| 上板联调 | **未做过**。真实串口模式现在连不上，因为固件不会枚举 CDC、不会应答握手 |

所以：**现在能演示的全部内容都在模拟模式下**，任何"实测数据"的说法都不成立。
真实链路的验收标准写在方案第 8 节（Windows 枚举成功、1/63/64/65/96/128/256 字节收发、
跨包分帧、拔插重连、按键与毫秒时基不被阻塞）。

## 运行

```bat
rem 模拟模式（不需要硬件，推荐先用这个验证界面）
run_host.cmd --sim

rem 真实串口：先在顶栏刷新并选中 COM 口，再点"连接"
run_host.cmd

rem 指定串口并自动连接
run_host.cmd --port COM5 --connect
```

Python 3.8+ 即可（开发用的是 3.12）。依赖：

```bat
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m pip install -r requirements.txt
```

`pyserial` 只影响真实串口；**没装 pyserial 也能跑模拟模式**（`serial_link` 里有 Windows 文件句柄兜底）。

## 测试

```bat
cd ghr\10.15\上位机
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -X utf8 -m unittest discover -s tests
```

* `tests/test_protocol.py` —— 分帧、校验、转义、会话去重、超长恢复、请求 id 池。
* `tests/test_client.py` —— 握手、心跳、超时策略、去重、坏帧统计、失联保护。
* `tests/test_sequence.py` —— 序列模型、校验、执行器状态机（含"到位才算完成"）。
* `tests/test_gui_smoke.py` —— 真的建 tkinter 窗口，用模拟后端跑一遍握手、发指令、
  遥测显示、序列运行与停止。无显示环境自动跳过。

## 文件

| 文件 | 作用 |
| --- | --- |
| `protocol.py` | 帧编解码、异或校验、分帧缓冲、会话去重、请求 id 池、参数默认值、错误码表 |
| `serial_link.py` | 串口枚举/打开/读写、`AutoReconnector` 退避重连；无 pyserial 时的兜底链路 |
| `host_client.py` | 把字节流变成状态与事件：握手、心跳、在途请求、超时策略、失联保护 |
| `simulator.py` | 固件行为的软件替身：一个字节进、字节出，按时间推进运动与到位事件 |
| `sequence.py` | 动作序列数据模型 + 执行器状态机 |
| `host_gui.py` | tkinter 主界面，只消费 `HostClient` 的事件 |
| `run_host.cmd` | 双击启动 |

## 必须守住的约定

1. **`ok` ≠ 到位。** 应答只代表固件接纳；完成要用 `#0,event,task,...,state=reached`，
   并用 `ok` 里回的 `task=<id>` 关联。界面上"已下发 / 到位 / 结果未知"是三种不同状态。
2. **没有回读的轴不显示实测值。** 固件在 `hello` 里声明 `readback=`；不在里面的轴，
   界面只显示"该轴未接回读"，遥测帧带 `readback=0`。
3. **运动类指令超时进入"结果未知"，不自动重发。** 只有 `ping/status/motor_query/param_get/hello`
   这类幂等查询才复用同一请求 id 重试，靠固件的会话去重防重复执行。
4. **停止字符 `!` 不走界面输入框。** 停止按钮发 `stop_all`；调试模式的十六进制输入禁止发 `!`。
5. **失联即停止下发。** 3 秒没有收到任何字节就进入"链路异常"，界面禁止继续操作并提示需要停止或重连。
6. **夹爪方向不反转。** `GRIPPER_OPEN_DEG=180`（松开）、`GRIPPER_GRAB_DEG=0`（夹紧），
   每次 45°，`servo.h` 里那对旧宏不参与计算。
7. **参数名以 `protocol.PARAM_DEFAULTS` 为准**，界面参数表与它一一对应（有测试守着）。
   固件实现 `param_get/param_set` 时必须用同一批名字。

## 还没做

* 固件侧：USB CDC 通信层、行协议解析、统一控制状态、非阻塞发送队列、Z 轴回读、遥测上报。
* PC 侧：序列模板（首次抓取流程）、曲线显示、PyInstaller 打包 exe、参数持久化到 Flash。
* 多轴回读的硬件方案（隔离/复用/RS-485）需要先确认驱动器 TXD 的输出结构。
