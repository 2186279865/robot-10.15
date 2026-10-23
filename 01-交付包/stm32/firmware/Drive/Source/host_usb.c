/* host_usb.c - 上位机链路传输层：STM32 OTG FS Device + CDC 虚拟串口
 *
 * 链路：板载 Type-C → STM32F407 原生 USB OTG FS → CDC → PC 虚拟 COM 口。
 *
 * 本文件是【独立编译单元】，要正式加入 Keil 工程的源文件列表（不是 inline 头文件）。
 * 依赖 ST 官方标准库 USB 库（STM32_USB-Host-Device_Lib_V2.2.0，MCD-ST Liberty
 * SW License Agreement V2），已放在 ghr/10.15/USB/ 下，未做任何修改；本工程自己
 * 写的只有 USB/App/usb_conf.h、USB/App/usb_bsp.c 和本文件。
 *
 * 层次与职责：
 *   USB 库（USB/）       ：USB 协议栈、OTG 底层、CDC 类、描述符
 *   host_usb.c（本文件） ：板级适配、CDC 接口回调、收发环形队列、
 *                          实现 Drive/Include/host_transport.h 的全部接口
 *   host_protocol_inline.h：行协议（由 main.c 包含），只通过 HostLink_* 收发字节
 *
 * 关键设计（对应 上位机方案-v3-TypeC-CDC.md 第 6 节）：
 *   1. 发送不另建缓冲：直接写 CDC 类自己的 APP_Rx_Buffer 环形队列，
 *      由库在 SOF 中断里按 64 字节分包发送（含整数包长的 ZLP 处理）。
 *      写入前先查剩余空间，放不下就整体失败，绝不留下半帧。
 *   2. 接收：CDC OUT 回调在 USB 中断里把字节复制进本文件的 512 字节环形队列；
 *      主循环用 HostLink_ReadByte() 逐字节取走。回调里不做任何阻塞操作。
 *   3. '!' 软件停止字符在 OUT 回调里被拦下并置锁存，不进入协议字节流；
 *      由协议层在主循环里 HostLink_TakeStopRequest() 取出。
 *   4. USB 复位/断开时清空两个队列与在途状态，避免把上一个会话的数据带进新会话。
 *   5. 队列是单生产者/单消费者：写指针只在中断里改，读指针只在主循环改；
 *      HostLink_QueueTx 更新 CDC 发送指针时用短临界区保护。
 */

/* Includes ------------------------------------------------------------------*/
#include "stm32f4xx.h"
#include "usb_conf.h"
#include "usb_core.h"
#include "usb_dcd.h"
#include "usb_dcd_int.h"
#include "usb_bsp.h"
#include "usbd_core.h"
#include "usbd_desc.h"
#include "usbd_cdc_core.h"
#include "usbd_conf.h"
#include "host_transport.h"

/* CDC 类内部的发送环形队列（由 usbd_cdc_core.c 定义，未导出到头文件里，
   和 ST 官方 VCP 例程 usbd_cdc_vcp.c 的做法一致，这里自己声明） */
extern uint8_t  APP_Rx_Buffer[];
extern uint32_t APP_Rx_ptr_in;
extern uint32_t APP_Rx_ptr_out;
extern uint32_t APP_Rx_length;
extern uint8_t  USB_Tx_State;

/* USB 设备句柄（唯一实例，app 层定义，中断处理函数与初始化都要用） */
USB_OTG_CORE_HANDLE USB_OTG_dev;

/* Private defines -----------------------------------------------------------*/
#define HOST_RX_RING_SIZE   512U                        /* 2 的幂，用掩码回绕 */
#define HOST_RX_RING_MASK   (HOST_RX_RING_SIZE - 1U)
#define HOST_CDC_LINE_CODING_LEN 7U                     /* dwDTERate+bChar+bParity+bData */
#define HOST_CDC_BITRATE_115200  115200UL

/* Private variables ---------------------------------------------------------*/
static volatile uint8_t  host_rx_ring[HOST_RX_RING_SIZE];
static volatile uint16_t host_rx_head;        /* 中断写 */
static volatile uint16_t host_rx_tail;        /* 主循环读 */
static volatile uint8_t  host_rx_err;
static volatile uint8_t  host_stop_request;
static volatile uint8_t  host_configured;
static uint8_t  host_line_coding[HOST_CDC_LINE_CODING_LEN];

/* Private function prototypes -----------------------------------------------*/
static uint16_t host_cdc_init(void);
static uint16_t host_cdc_deinit(void);
static uint16_t host_cdc_ctrl(uint32_t cmd, uint8_t *buf, uint32_t len);
static uint16_t host_cdc_data_tx(void);
static uint16_t host_cdc_data_rx(uint8_t *buf, uint32_t len);

/* CDC 接口回调表：usbd_conf.h 里 APP_FOPS 指向它 */
CDC_IF_Prop_TypeDef host_cdc_fops =
{
  host_cdc_init,
  host_cdc_deinit,
  host_cdc_ctrl,
  host_cdc_data_tx,
  host_cdc_data_rx
};

/* Private functions ---------------------------------------------------------*/

/**
  * @brief  清空接收队列、错误位与停止锁存；在 USB 复位/断开时调用
  * @note   可在中断上下文调用
  */
static void host_usb_reset_buffers(void)
{
  host_rx_head = 0U;
  host_rx_tail = 0U;
  host_rx_err = 0U;
  host_stop_request = 0U;

  /* CDC 发送队列也一并复位，避免把上一个会话的残留字节发给新会话 */
  APP_Rx_ptr_in = 0U;
  APP_Rx_ptr_out = 0U;
  APP_Rx_length = 0U;
  USB_Tx_State = 0U;   /* USB_CDC_IDLE */
}

/**
  * @brief  CDC 接口初始化（SET_CONFIGURATION 时由 CDC 类调用）
  */
static uint16_t host_cdc_init(void)
{
  return (uint16_t)USBD_OK;
}

/**
  * @brief  CDC 接口反初始化
  */
static uint16_t host_cdc_deinit(void)
{
  host_usb_reset_buffers();
  return (uint16_t)USBD_OK;
}

/**
  * @brief  CDC 类请求回调：线路编码与调制解调器信号
  * @param  cmd: CDC 请求码
  * @param  buf: 数据缓冲（GET_LINE_CODING 时要由本函数填写）
  * @param  len: 数据长度
  * @note   USB 虚拟串口的"波特率"只是形式，USB 全速固定 12Mbps；
  *         这里如实记录主机下发的线路编码，不据此改变任何硬件。
  */
static uint16_t host_cdc_ctrl(uint32_t cmd, uint8_t *buf, uint32_t len)
{
  uint32_t i;

  switch (cmd)
  {
    case 0x20U: /* SET_LINE_CODING */
      if ((buf != 0) && (len >= HOST_CDC_LINE_CODING_LEN))
      {
        for (i = 0U; i < HOST_CDC_LINE_CODING_LEN; i++)
        {
          host_line_coding[i] = buf[i];
        }
      }
      break;

    case 0x21U: /* GET_LINE_CODING */
      if ((buf != 0) && (len >= HOST_CDC_LINE_CODING_LEN))
      {
        for (i = 0U; i < HOST_CDC_LINE_CODING_LEN; i++)
        {
          buf[i] = host_line_coding[i];
        }
      }
      break;

    case 0x22U: /* SET_CONTROL_LINE_STATE：DTR/RTS，记录意义不大，这里只接受 */
    default:
      break;
  }

  return (uint16_t)USBD_OK;
}

/**
  * @brief  应用层主动推数据回调：本实现不从这里发送
  * @note   发送走 APP_Rx_Buffer 环形队列（见 HostLink_QueueTx），
  *         当前版本的 CDC 类也不会调用本回调，保留以满足回调表结构。
  */
static uint16_t host_cdc_data_tx(void)
{
  return (uint16_t)USBD_OK;
}

/**
  * @brief  USB OUT 回调：在中断上下文把收到的字节搬进接收队列
  * @param  buf: 本次收到的数据
  * @param  len: 字节数
  * @note   只允许做复制与置标志，严禁 Delay_ms 之类依赖 SysTick 的阻塞调用。
  */
static uint16_t host_cdc_data_rx(uint8_t *buf, uint32_t len)
{
  uint32_t i;
  uint16_t head = host_rx_head;

  for (i = 0U; i < len; i++)
  {
    uint8_t  b = buf[i];
    uint16_t next;

    /* 软件停止字符：拦下并锁存，不进入协议字节流 */
    if (b == (uint8_t)'!')
    {
      host_stop_request = 1U;
      continue;
    }

    next = (uint16_t)((head + 1U) & HOST_RX_RING_MASK);
    if (next == host_rx_tail)
    {
      /* 队列满：丢弃并记溢出（主循环太慢时才会发生） */
      host_rx_err |= HOST_LINK_RXERR_OVERRUN;
      continue;
    }

    host_rx_ring[head] = b;
    head = next;
  }

  host_rx_head = head;
  return (uint16_t)USBD_OK;
}

/* ---- ST USB 库的用户回调（USR_cb），放在本文件里，省掉评估板用的 usbd_usr.c ---- */

/**
  * @brief  USB 库初始化完成回调
  */
static void host_usr_init(void)
{
}

/**
  * @brief  USB 复位回调（会发生在枚举开始时，也可能发生在异常拔插后）
  * @param  speed: 设备速度
  */
static void host_usr_device_reset(uint8_t speed)
{
  (void)speed;
  host_configured = 0U;
  host_usb_reset_buffers();
}

/**
  * @brief  主机已完成 SET_CONFIGURATION：CDC 数据接口就绪
  */
static void host_usr_device_configured(void)
{
  host_configured = 1U;
}

/**
  * @brief  USB 挂起（总线空闲）
  */
static void host_usr_device_suspended(void)
{
}

/**
  * @brief  USB 恢复
  */
static void host_usr_device_resumed(void)
{
}

/**
  * @brief  设备接入主机
  */
static void host_usr_device_connected(void)
{
}

/**
  * @brief  设备与主机断开
  */
static void host_usr_device_disconnected(void)
{
  host_configured = 0U;
  host_usb_reset_buffers();
}

USBD_Usr_cb_TypeDef USR_cb =
{
  host_usr_init,
  host_usr_device_reset,
  host_usr_device_configured,
  host_usr_device_suspended,
  host_usr_device_resumed,
  host_usr_device_connected,
  host_usr_device_disconnected
};

/* Exported functions --------------------------------------------------------*/

/**
  * @brief  OTG FS 中断入口（向量表里是弱定义，这里给出真实实现）
  */
void OTG_FS_IRQHandler(void)
{
  (void)USBD_OTG_ISR_Handler(&USB_OTG_dev);
}

/**
  * @brief  初始化 USB 外设与两个队列，上电调用一次
  * @note   调用前 SystemInit 已把 PLL 配好（PLLQ=7 → USB 需要 48MHz）。
  *         本函数不阻塞等待枚举，枚举过程全部由中断完成。
  */
void HostLink_Init(void)
{
  host_rx_head = 0U;
  host_rx_tail = 0U;
  host_rx_err = 0U;
  host_stop_request = 0U;
  host_configured = 0U;

  /* 默认线路编码 115200 8N1（USB 虚拟串口不依赖它，只是让 GET_LINE_CODING 有值） */
  host_line_coding[0] = (uint8_t)(HOST_CDC_BITRATE_115200 & 0xFFU);
  host_line_coding[1] = (uint8_t)((HOST_CDC_BITRATE_115200 >> 8) & 0xFFU);
  host_line_coding[2] = (uint8_t)((HOST_CDC_BITRATE_115200 >> 16) & 0xFFU);
  host_line_coding[3] = (uint8_t)((HOST_CDC_BITRATE_115200 >> 24) & 0xFFU);
  host_line_coding[4] = 0x00U;  /* 1 位停止位 */
  host_line_coding[5] = 0x00U;  /* 无校验 */
  host_line_coding[6] = 0x08U;  /* 8 位数据 */

  USBD_Init(&USB_OTG_dev,
            USB_OTG_FS_CORE_ID,
            &USR_desc,
            &USBD_CDC_cb,
            &USR_cb);
}

/**
  * @brief  主循环轮询：目前 USB 状态变化都由中断回调直接更新，
  *         这里只做兜底检查（被主机取消配置时清标志）
  */
void HostLink_Poll(void)
{
  if ((host_configured != 0U) &&
      (USB_OTG_dev.dev.device_status != (uint8_t)USB_OTG_CONFIGURED))
  {
    host_configured = 0U;
    host_usb_reset_buffers();
  }
}

/**
  * @brief  返回 1 表示主机已枚举并配置（不代表 PC 应用已连接）
  */
uint8_t HostLink_IsConfigured(void)
{
  return host_configured;
}

/**
  * @brief  从接收队列取一个字节
  * @param  byte: 输出
  * @retval 1=取到，0=队列为空（不阻塞）
  */
uint8_t HostLink_ReadByte(uint8_t *byte)
{
  uint16_t tail;

  if (byte == 0)
  {
    return 0U;
  }

  tail = host_rx_tail;
  if (tail == host_rx_head)
  {
    return 0U;
  }

  *byte = host_rx_ring[tail];
  host_rx_tail = (uint16_t)((tail + 1U) & HOST_RX_RING_MASK);
  return 1U;
}

/**
  * @brief  把一整帧交给链路发送：先查空间，再整体复制
  * @param  data: 待发送字节
  * @param  len : 字节数
  * @retval HOST_LINK_OK / HOST_LINK_ERR_PARAM / HOST_LINK_ERR_NOT_READY / HOST_LINK_ERR_FULL
  * @note   返回 OK 时数据已复制，调用方可以立刻复用缓冲区；失败时队列未被改动。
  */
uint8_t HostLink_QueueTx(const uint8_t *data, uint16_t len)
{
  uint32_t in;
  uint32_t out;
  uint32_t free_bytes;
  uint16_t i;

  if ((data == 0) || (len == 0U))
  {
    return HOST_LINK_ERR_PARAM;
  }

  if (host_configured == 0U)
  {
    return HOST_LINK_ERR_NOT_READY;
  }

  /* 短临界区：USB 中断里的 SOF 处理会读 APP_Rx_ptr_in/out */
  __disable_irq();

  in  = APP_Rx_ptr_in;
  out = APP_Rx_ptr_out;

  if (in >= out)
  {
    free_bytes = (uint32_t)APP_RX_DATA_SIZE - 1U - (in - out);
  }
  else
  {
    free_bytes = out - in - 1U;
  }

  if ((uint32_t)len > free_bytes)
  {
    __enable_irq();
    return HOST_LINK_ERR_FULL;
  }

  for (i = 0U; i < len; i++)
  {
    APP_Rx_Buffer[in] = data[i];
    in++;
    if (in >= (uint32_t)APP_RX_DATA_SIZE)
    {
      in = 0U;
    }
  }
  APP_Rx_ptr_in = in;

  __enable_irq();

  return HOST_LINK_OK;
}

/**
  * @brief  取出并清零链路接收错误位
  */
uint8_t HostLink_TakeRxError(void)
{
  uint8_t e = host_rx_err;

  host_rx_err = 0U;
  return e;
}

/**
  * @brief  取出并清零软件停止字符锁存
  */
uint8_t HostLink_TakeStopRequest(void)
{
  uint8_t s = host_stop_request;

  host_stop_request = 0U;
  return s;
}

/************************ (C) COPYRIGHT STMicroelectronics *****END OF FILE****/
