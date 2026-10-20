/* host_transport.h - 上位机链路传输层接口
 *
 * 链路：板载 Type-C → STM32 USB OTG FS Device → CDC 虚拟串口 → PC。
 * 不是 USART1 + USB 转 TTL（那是 v1/v2 方案，已作废）。
 *
 * 分层：
 *   Drive/Source/host_usb.c        独立编译单元，正式加入 Keil 工程；
 *                                  内含 ST USB OTG 库的板级适配与 CDC 类驱动，
 *                                  并实现本文件声明的全部函数。
 *   Drive/Include/host_protocol_inline.h
 *                                  业务层，由 main.c 包含；只通过本文件收发字节，
 *                                  不直接碰 USB 寄存器。
 *
 * 约定（见 上位机方案-v3-TypeC-CDC.md 第 6 节）：
 *   1. HostLink_QueueTx 必须先复制进自有队列再返回成功；失败时不允许留下半帧。
 *   2. HostLink_IsConfigured 只代表 USB 已被主机枚举，不代表 PC 应用已连接；
 *      必须完成 hello 握手后才接受运动命令。
 *   3. HostLink_ReadByte 由协议层在主循环里轮询，一次取一个字节，不允许阻塞。
 *   4. USB OUT 回调里只允许：扫描停止字符 '!'、把字节复制进接收队列、
 *      登记长度与错误。不得调用 Delay_ms 这类依赖 SysTick 的阻塞函数。
 *   5. 收发队列是单生产者（USB 中断）/单消费者（主循环），临界区要短。
 *   6. USB reset / 取消配置 / 拔线时，要丢弃半帧与过期应答，并复位队列。
 */

#ifndef HOST_TRANSPORT_H
#define HOST_TRANSPORT_H

#include <stdint.h>

/* ---- HostLink_QueueTx 返回值 ---- */
#define HOST_LINK_OK            0U  /* 已复制进发送队列 */
#define HOST_LINK_ERR_PARAM     1U  /* 空指针或 len 为 0 */
#define HOST_LINK_ERR_NOT_READY 2U  /* USB 未配置，调用方稍后重试 */
#define HOST_LINK_ERR_FULL      3U  /* 发送队列放不下这一帧 */

/* ---- HostLink_TakeRxError 的错误位，取出即清零，可叠加 ---- */
#define HOST_LINK_RXERR_OVERRUN 0x01U  /* 接收队列溢出，已丢字节 */
#define HOST_LINK_RXERR_FRAMING 0x02U  /* USB 层帧错误（短包/ZLP 异常等） */

/* 队列容量：接收侧至少要放下 4 帧 × 96 字节，避免主循环偶尔变慢就丢命令。
 * 这两个宏只描述建议值，实际缓冲区在 host_usb.c 里定义。 */
#define HOST_LINK_RX_CAPACITY   512U
#define HOST_LINK_TX_CAPACITY   512U

/* 初始化 USB 外设、板级引脚与两个队列。上电调用一次，之后 HostLink_Poll() 驱动。 */
void    HostLink_Init(void);

/* 主循环调用：处理 USB 状态变化（枚举、复位、挂起）与结束点搬移。 */
void    HostLink_Poll(void);

/* 返回 1 表示 USB 已被主机枚举并配置（CDC 数据接口已就绪）。 */
uint8_t HostLink_IsConfigured(void);

/* 从接收队列取一个字节。返回 1=取到，0=队列为空。主循环轮询，不阻塞。 */
uint8_t HostLink_ReadByte(uint8_t *byte);

/* 把一整帧交给链路发送。成功前已复制，调用方可以立刻复用缓冲区。 */
uint8_t HostLink_QueueTx(const uint8_t *data, uint16_t len);

/* 取出并清零链路层累计的接收错误位（见 HOST_LINK_RXERR_*）。 */
uint8_t HostLink_TakeRxError(void);

/* 取出并清零"软件停止请求"锁存。
 * 方案的停止字符 '!' 是在 USB OUT 回调里扫描并置位的，传输层把它保存下来，
 * 由协议层在主循环里取出，再走统一停止流程。 */
uint8_t HostLink_TakeStopRequest(void);

#endif /* HOST_TRANSPORT_H */
