/**
  ******************************************************************************
  * @file    usb_bsp.c
  * @brief   USB OTG FS 板级支持包（本工程专用）
  *
  * 取代 ST 官方 VCP 例程里的评估板 BSP（原文件见 App/_st_eval_reference/usb_bsp.c）。
  * 本板情况：
  *   - 板载 Type-C：D- 经 0Ω 到 PA11，D+ 经 0Ω 到 PA12（AF10 = OTG_FS）
  *   - PA9(VBUS) 未接线，因此不定义 VBUS_SENSING_ENABLED（见 usb_conf.h）
  *   - 主晶振 8MHz，PLLQ=7 → USB 48MHz（由 SystemInit 配好，本文件不碰时钟树）
  *   - 不使用 TIM2：TIM2 已被 stepper 的 1ms 毫秒时基占用
  *
  * 微秒延时用 DWT 周期计数器实现，不占用任何定时器；毫秒延时复用 delay.c。
  ******************************************************************************
  */

/* Includes ------------------------------------------------------------------*/
#include "usb_bsp.h"
#include "usb_core.h"
#include "stm32f4xx.h"
#include "delay.h"

/* Private variables ---------------------------------------------------------*/
static uint8_t dwt_ready = 0;

/* Private functions ---------------------------------------------------------*/

/**
  * @brief  打开 DWT 周期计数器，供微秒延时使用
  */
static void usb_bsp_dwt_enable(void)
{
  if (dwt_ready)
  {
    return;
  }
#if defined(DWT) && defined(CoreDebug)
  CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
  DWT->CYCCNT = 0;
  DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
  dwt_ready = 1;
#endif
}

/* Exported functions --------------------------------------------------------*/

/**
  * @brief  老的评估板 BSP 入口，本工程不需要初始化 LCD/LED/按键，保留空实现
  */
void BSP_Init(void)
{
}

/**
  * @brief  配置 GPIO 与 OTG_FS 时钟
  * @param  pdev: 设备句柄
  */
void USB_OTG_BSP_Init(USB_OTG_CORE_HANDLE *pdev)
{
  GPIO_InitTypeDef GPIO_InitStructure;

  /* 打开 OTG_FS 时钟（AHB2） */
  RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOA, ENABLE);
  RCC_AHB2PeriphClockCmd(RCC_AHB2Periph_OTG_FS, ENABLE);

  /* PA11 = OTG_FS_DM, PA12 = OTG_FS_DP */
  GPIO_InitStructure.GPIO_Pin   = GPIO_Pin_11 | GPIO_Pin_12;
  GPIO_InitStructure.GPIO_Mode  = GPIO_Mode_AF;
  GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;
  GPIO_InitStructure.GPIO_OType = GPIO_OType_PP;
  GPIO_InitStructure.GPIO_PuPd  = GPIO_PuPd_NOPULL;
  GPIO_Init(GPIOA, &GPIO_InitStructure);

  GPIO_PinAFConfig(GPIOA, GPIO_PinSource11, GPIO_AF_OTG1_FS);
  GPIO_PinAFConfig(GPIOA, GPIO_PinSource12, GPIO_AF_OTG1_FS);

  (void)pdev;
}

/**
  * @brief  使能 OTG_FS 中断
  * @param  pdev: 设备句柄
  * @note   抢占优先级 2 低于 stepper 的 TIM2(1)，USB 中断不会打断毫秒时基
  */
void USB_OTG_BSP_EnableInterrupt(USB_OTG_CORE_HANDLE *pdev)
{
  NVIC_InitTypeDef NVIC_InitStructure;

  NVIC_InitStructure.NVIC_IRQChannel = OTG_FS_IRQn;
  NVIC_InitStructure.NVIC_IRQChannelPreemptionPriority = 2;
  NVIC_InitStructure.NVIC_IRQChannelSubPriority = 0;
  NVIC_InitStructure.NVIC_IRQChannelCmd = ENABLE;
  NVIC_Init(&NVIC_InitStructure);

  (void)pdev;
}

/**
  * @brief  微秒级延时（DWT 周期计数器）
  * @param  usec: 微秒数
  */
void USB_OTG_BSP_uDelay(const uint32_t usec)
{
  uint32_t start;
  uint32_t ticks;

  usb_bsp_dwt_enable();

#if defined(DWT) && defined(CoreDebug)
  if (dwt_ready)
  {
    start = DWT->CYCCNT;
    ticks = (uint32_t)(((uint64_t)usec * (uint64_t)(SystemCoreClock / 1000000U)));
    if (ticks == 0U)
    {
      ticks = 1U;
    }
    while ((uint32_t)(DWT->CYCCNT - start) < ticks)
    {
      /* busy wait */
    }
    return;
  }
#endif

  /* DWT 不可用时的兜底：按 168MHz 粗算的忙等 */
  {
    volatile uint32_t n = usec * 21U;
    while (n != 0U)
    {
      n--;
    }
  }
}

/**
  * @brief  毫秒级延时（复用 delay.c 的 SysTick 毫秒延时）
  * @param  msec: 毫秒数
  */
void USB_OTG_BSP_mDelay(const uint32_t msec)
{
  if (msec == 0U)
  {
    return;
  }
  Delay_ms(msec);
}

/**
  * @brief  OTG 定时器中断回调，本工程未使用
  */
void USB_OTG_BSP_TimerIRQ(void)
{
}

/**
  * @brief  设备模式下不需要驱动 VBUS
  */
void USB_OTG_BSP_ConfigVBUS(USB_OTG_CORE_HANDLE *pdev)
{
  (void)pdev;
}

/**
  * @brief  设备模式下不需要驱动 VBUS
  */
void USB_OTG_BSP_DriveVBUS(USB_OTG_CORE_HANDLE *pdev, uint8_t state)
{
  (void)pdev;
  (void)state;
}

/**
  * @brief  恢复（device mode 无外部收发器动作）
  */
void USB_OTG_BSP_Resume(USB_OTG_CORE_HANDLE *pdev)
{
  (void)pdev;
}

/**
  * @brief  挂起（device mode 无外部收发器动作）
  */
void USB_OTG_BSP_Suspend(USB_OTG_CORE_HANDLE *pdev)
{
  (void)pdev;
}

/************************ (C) COPYRIGHT STMicroelectronics *****END OF FILE****/
