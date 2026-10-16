/**
  ******************************************************************************
  * @file    usb_conf.h
  * @brief   USB OTG 配置（本工程专用，替换 ST 官方 VCP 例程的评估板配置）
  *
  * 本文件由机器人抓取系统项目组编写，基于 ST 官方
  * STM32_USB-Host-Device_Lib_V2.2.0 中 Project/USB_Device_Examples/VCP/inc/usb_conf.h
  * （MCD-ST Liberty SW License Agreement V2）改写：去掉评估板依赖，只保留
  * 全速设备模式需要的配置。原始评估板版本见 App/_st_eval_reference/usb_conf.h。
  ******************************************************************************
  */

/* Define to prevent recursive inclusion -------------------------------------*/
#ifndef __USB_CONF__H__
#define __USB_CONF__H__

/* Includes ------------------------------------------------------------------*/
#include "stm32f4xx.h"

/*----------------------------------------------------------------------------*/
/* 工作模式：只用 FS 设备模式                                                   */
/*   - 板载 Type-C 的 D-/D+ 接 PA11/PA12（AF10），走 OTG_FS                    */
/*   - 不定义 USE_USB_OTG_HS / USB_OTG_HS_CORE / USB_OTG_HS_INTERNAL_DMA_ENABLED */
/*----------------------------------------------------------------------------*/
#define USE_USB_OTG_FS
#ifdef USE_USB_OTG_FS
 #define USB_OTG_FS_CORE
#endif

#define USE_DEVICE_MODE

/*----------------------------------------------------------------------------*/
/* VBUS 检测                                                                   */
/*   交接材料记录 PA9(VBUS) 未接线，因此这里【故意不定义】VBUS_SENSING_ENABLED， */
/*   由 usb_core.c 走 NOVBUSSENS 分支（B-session 视为有效），插上 Type-C 即可    */
/*   被主机枚举。若以后把 VBUS 接到 PA9，再打开下面的宏重新验证。                 */
/*----------------------------------------------------------------------------*/
/* #define VBUS_SENSING_ENABLED */

/*----------------------------------------------------------------------------*/
/* FS FIFO 分配（单位：32 位字；OTG_FS 专用 RAM 共 320 字 = 1280 字节）          */
/*   128 + 32 + 128 + 32 + 0 = 320                                             */
/*----------------------------------------------------------------------------*/
#ifdef USB_OTG_FS_CORE
 #define RX_FIFO_FS_SIZE                          128
 #define TX0_FIFO_FS_SIZE                          32   /* EP0            */
 #define TX1_FIFO_FS_SIZE                         128   /* CDC 数据 IN    */
 #define TX2_FIFO_FS_SIZE                          32   /* CDC 命令 IN    */
 #define TX3_FIFO_FS_SIZE                           0
#endif

/* 主机模式（本工程不使用）也要有定义，否则 usb_core.c 的 FS 分支编译不过 */
#define TXH_NP_FS_FIFOSIZ                          64
#define TXH_P_FS_FIFOSIZ                           64

/*----------------------------------------------------------------------------*/
/* 编译期自检                                                                  */
/*----------------------------------------------------------------------------*/
#ifndef USB_OTG_FS_CORE
 #error "USB_OTG_FS_CORE should be defined (this project is FS device only)"
#endif
#ifndef USE_DEVICE_MODE
 #error "USE_DEVICE_MODE should be defined"
#endif

/*----------------------------------------------------------------------------*/
/* 对齐与打包宏（未启用 HS 内部 DMA，因此对齐宏为空）                            */
/*----------------------------------------------------------------------------*/
#define __ALIGN_BEGIN
#define __ALIGN_END

#if defined (__CC_ARM)          /* ARM Compiler (ARMCC5) */
  #define __packed    __packed
#elif defined (__ICCARM__)      /* IAR Compiler */
  #define __packed    __packed
#elif defined (__GNUC__)        /* GNU Compiler */
  #define __packed    __attribute__ ((__packed__))
#elif defined (__TASKING__)     /* TASKING Compiler */
  #define __packed    __unaligned
#endif

#endif /* __USB_CONF__H__ */

/************************ (C) COPYRIGHT STMicroelectronics *****END OF FILE****/
