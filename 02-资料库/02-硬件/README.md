# 02-硬件

> 详细接线/编译/烧录都在 `../../01-交付包/stm32/docs/烧录与编译.md` 和 `../../01-交付包/README.md` §4。
> 这里只放"资料性"的内容：整机清单、接线速查、坑。

## 整机清单

| 件 | 型号/规格 | 备注 |
|---|---|---|
| 主控 | STM32F407ZGT6（鹿小班 LXB407ZG-P1 板） | 168 MHz，1 MB Flash |
| 驱动器 ×4 | 张大头 EMM V5 步进闭环 | 地址 Z=1、托盘=3、X=4、Y=5；12–48 V 独立供电 |
| 舵机 | 50 Hz PWM，500–2500 us ↔ 0–270° | PB6 = TIM4_CH1，4.8–7.4 V 独立供电 |
| 相机 ×2 | 通用 USB HD Camera（`1e45:8022`，MJPG） | 一台顶视（正俯视托盘）、一台侧视 |
| 上位机边缘端 | 香橙派 4 Pro 12G（A733，Ubuntu 26.04 / Python 3.14） | 只做采集 + 串口桥；也可用树莓派 |
| 笔记本 | Windows + RTX 5060（8 GB） | 跑视觉 + 上位机 |
| 网络 | 同一 WiFi / 手机热点 / 便携路由器 | 需要互 ping 通；AP 隔离会挡 UDP 发现 |

## 接线速查（STM32 侧）

| 功能 | 引脚 | 说明 |
|---|---|---|
| 步进串口 | PA2 = USART2_TX | 115200 8N1，只发不收 |
| 编码器回读 | PA3 = USART2_RX | **未接线** → 所以四轴 `motor_query` 一律 `E04` |
| 舵机 | PB6 = TIM4_CH1 | PSC=83, ARR=19999 |
| 按键 | PA0 | 必须内部下拉；按下读到高 |
| LED | PC13 | 低电平点亮 |
| 上位机串口 | USB CDC（0483:5740） | 插上即 COMx / `/dev/ttyACM0` |
| 调试串口 | PA9/PA10 = USART1 | 预留，当前未用 |

## 香橙派侧（USB 拓扑，实测）

```
Bus 001 (xhci, USB3)  ── USB HD Camera A ── /dev/video0 (+video1=元数据)
Bus 003 (ehci, USB2)  ── Hub ── USB HD Camera B ── /dev/video2 (+video3)
                      └── STM32 CDC ── /dev/ttyACM0
```

- **两台相机挂同一个 USB2 hub 会掉到 5–6 fps**（带宽不够）；分开插（一台走 USB3）能到 11 fps。
- 相机槽位（`0`/`1` 哪个是顶视）**每次插拔可能变**，用 `01-交付包/net/find_pi.py <IP> --full --cam 0/1` 现场确认，再改 `01-交付包/vision/config.json` 的 `remote.cam_map`。
- 相机节点规律：每台相机占两个 `/dev/videoN`，只有偶数那个能抓帧（`video0`/`video2`）。

## 电源

- 驱动器：独立 12–48 V（本机 24 V）；
- 舵机：4.8–7.4 V（**不要**从 STM32 的 3.3/5 V 取电）；
- **必须共地**：STM32 GND、驱动器 GND、舵机电源 GND 全部连一起，否则串口帧乱、舵机抖。

## 烧录入口

- 现成：`01-交付包/stm32/hex/STM32F429.hex`
- 源码：`01-交付包/stm32/`
- 说明：`01-交付包/stm32/docs/烧录与编译.md`
