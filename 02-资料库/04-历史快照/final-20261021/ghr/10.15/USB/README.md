# ST USB OTG 设备库（V2.2.0）移植说明

上位机要用的 **板载 Type-C → STM32 USB OTG FS Device → CDC 虚拟串口** 就靠这份库实现。
方案见 `../上位机方案-v3-TypeC-CDC.md` 第 5、6、10 节。

## 来源与许可

- 来源：GitHub 镜像仓库 `jys0305/STM32_USB-Host-Device_Lib_V2.2.0`（分支 master），
  即 ST 官方发行包 **STM32_USB-Host-Device_Lib_V2.2.0** 的原始内容，只取下面列出的文件。
  抓取脚本留在 `_fetch_st_usb_lib.ps1`（记录原始 raw URL，便于日后核对/补齐）。
- 许可：**MCD-ST Liberty SW License Agreement V2**，许可证原文随库放在
  `MCD-ST_Liberty_SW_License_Agreement_V2.pdf`。每个源文件头部都保留 ST 的版权声明。
- 为什么不用别的：Keil 的 MDK-Middleware USB 依赖 CMSIS-Driver/RTX，和本工程裸 StdPeriph 不兼容；
  桌面上那份 `HAL库\7.USB-虚拟串口` 是 HAL 版，按方案要求**不跨代拼接、不套 HAL API**，只能当结构参考。

## 目录

```
USB/
  STM32_USB_OTG_Driver/{inc,src}          ST 底层驱动（原样，未改）
  STM32_USB_Device_Library/Core/{inc,src} 设备核心（原样，未改）
  STM32_USB_Device_Library/Class/cdc/{inc,src} CDC 类（原样，未改）
  App/usb_conf.h      本工程自己写的（不是 ST 的 eval 版）：只启用 FS + Device 模式
  App/usbd_conf.h     由 ST 版改：APP_FOPS 改名 host_cdc_fops，端点/缓冲尺寸保持 ST 值
  App/usbd_desc.c/h   由 ST 版改：VID/PID 与字符串换成这个项目
  App/usb_bsp.c       本工程自己写的：PA11/PA12、OTG_FS 时钟与中断、DWT 微秒延时
  App/_st_eval_reference/   ST 评估板原版参考文件，**不参与编译**，只作对照
  Drive/Source/host_usb.c   适配层（在 Drive 目录里，不在本目录）
```

`App/_st_eval_reference/` 含 ST 的 `usb_conf.h`、`usb_bsp.c`、`usbd_usr.c`、`usbd_cdc_vcp.c/h`：
本工程**没有**用它们（本工程自己实现了 BSP 和 CDC 胶水），留下来是为了对照 ST 原版写法。

## 参与编译的文件（Keil 工程 `USB` 组 + `Drive` 组里的 host_usb.c，共 10 个）

`usb_core.c`、`usb_dcd.c`、`usb_dcd_int.c`、`usbd_core.c`、`usbd_ioreq.c`、`usbd_req.c`、
`usbd_cdc_core.c`、`usbd_desc.c`、`usb_bsp.c`、`../../Drive/Source/host_usb.c`。

**故意不加进工程**：`_st_eval_reference/` 下全部文件、`usbd_cdc_vcp.c`（ST 示例胶水，
本工程用 `host_usb.c` 替代）。加了会重复定义 `VCP_fops` / `USR_cb`。

## 关键配置决定

| 项目 | 取值 | 原因 |
| --- | --- | --- |
| 模式 | `USE_USB_OTG_FS` + `USB_OTG_FS_CORE` + `USE_DEVICE_MODE` | 板载 Type-C 接的是 OTG FS（PA11 D−、PA12 D+，AF10） |
| VBUS 检测 | **不定义** `VBUS_SENSING_ENABLED` | PA9 没接 VBUS；不定义时核心会置 `NOVBUSSENS`，枚举不依赖 VBUS |
| FIFO | RX 128 / TX0 32 / TX1 128 / TX2 32 / TX3 0 | CDC 数据 IN 用 TX1，命令 IN 用 TX2；`TXH_NP_FS_FIFOSIZ`/`TXH_P_FS_FIFOSIZ` 必须定义（`usb_core.c` 的 FS 分支无条件下引用），给 64 |
| 端点 | `CDC_IN_EP 0x81`、`CDC_OUT_EP 0x01`、`CDC_CMD_EP 0x82` | 与 `usbd_desc.c` 里的配置描述符以及 `usb_conf.h` 的 FIFO 编号一一对应 |
| 包长 | `CDC_DATA_MAX_PACKET_SIZE 64`、`CDC_CMD_PACKET_SZE 8` | 全速设备最大值；64 的整数倍短包要发 ZLP |
| 发送节奏 | `CDC_IN_FRAME_INTERVAL 5` | 异步发送模型：CDC 类在 SOF 中断里每 5 帧搬一次 `APP_Rx_Buffer`，约 64B/5ms |
| CDC 胶水 | `usbd_conf.h` 里 `#define APP_FOPS host_cdc_fops` | `usbd_cdc_core.c` 通过 `extern CDC_IF_Prop_TypeDef APP_FOPS;` 回调；实现放在 `host_usb.c` |
| 描述符 | VID `0x0483` / PID `0x5740`，产品串 "Robot Gripper CDC" | ST 虚拟串口默认组合，Windows 10 直接绑 `usbser`；换成自定义 VID/PID 要装驱动 |
| 中断 | `OTG_FS_IRQHandler` 抢占优先级 2（TIM2 是 1） | 毫秒时基优先；中断里**不许**调用靠 SysTick 的 `Delay_ms` |

## 收发的三个坑（已在 `host_usb.c` 里处理）

1. **CDC 类自己管发送缓冲区**：`usbd_cdc_core.c` 里有全局 `APP_Rx_Buffer[2048]`、
   `APP_Rx_ptr_in/out`、`APP_Rx_length`、`USB_Tx_State`，由 SOF 中断里的 `Handle_USBAsynchXfer()`
   自动搬运。所以 `HostLink_QueueTx()` 不用 `DCD_EP_Tx`，而是**直接写这个环形缓冲**并做
   剩余空间检查；空间不够就整体拒绝（不写半帧）。写入前后关中断的临界区要短。
2. **64 字节包会切开命令**：主机一次发的 96 字节帧会跨包到达，`HostLink_ReadByte()` 只负责
   把字节取走，分帧完全交给 `host_protocol_inline.h`（逐字节状态机）。
3. **拔线/复位**：`HostLink_Poll()` 检查 `USB_OTG_dev.dev.device_status`，不是
   `USB_OTG_CONFIGURED` 就清空收发队列、半帧和过期应答，避免"复位后重放旧运动"。

## 尚未验证（截至 2026-10-16）

- **没有上板、没有枚举过**。已完成的验证只有：ARMCC5 全量编译 0 error 0 warning（正式工程
  `Code=23644`）、链接后 `OTG_FS_IRQHandler`/`USBD_Init`/`USBD_CDC_cb`/`HostLink_*`/
  `host_protocol_task` 都在符号表里、协议层在 PC 上用主机编译器逐字节跑通 238 项检查。
- 首次上板要确认：Windows 能否枚举出虚拟 COM 口、`hello` 能否握手、拔插后能否重连。
- CC1/CC2 悬空时请用 **USB-A 转 Type-C** 线；若要用 C-to-C，需先确认两脚各经约 5.1kΩ 到地
  （不能把 CC1/CC2 短接）。
