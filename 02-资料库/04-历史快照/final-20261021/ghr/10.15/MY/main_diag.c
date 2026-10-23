#include "stm32f4xx.h"
#include "led.h"
#include "delay.h"
#include "gripper.h"
#include "key.h"
#include "stepper.h"

/* 模块实现内联（和 main.c 一样，本工程不把单独的 .c 加进 Keil 工程）。 */
#include "gripper_inline.h"
#include "key_inline.h"
#include "stepper_inline.h"

/* ================== 步进硬件诊断：位置模式 FD 走半圈 ==================
 * 这是**单独的一支诊断固件**，和 main.c 不是同一套程序：
 *   - main.c        ：PA0 按一下 1 号电机转、再按一下停（正式功能）
 *   - main_diag.c   ：本文件。上电后反复"每档参数走半圈 -> 停 2.5 秒"，
 *                     用来把"硬件不通"和"参数不对"分开。
 *
 * 为什么用 FD 位置模式而不是 F6 速度模式：
 *   位置模式在例36 上被实测验证过（Z 轴 20 圈就是 FD 发的），而且只发一次、
 *   驱动器自己走完，不存在"速度模式下加速度档位太大导致起转力矩不足"的问题。
 *   FD 能转 = 驱动器、电机电源、共地、地址、接线都没问题，剩下的才是参数。
 *
 * 观察方法：看电机（或听声音）。每走一步后 LED 会慢眨 2 下、然后停 2.5 秒。
 *   电机跟着这个节奏一动一动 = 硬件 OK（记下这一档 RPM）；全程纹丝不动
 *   = 硬件/接线问题，重点查下面 4 条。
 *
 * 硬件必查（代码解决不了）：
 *   1. 驱动器接了 12~48V 电机电源吗？USB/5V 供电电机不动。
 *   2. STM32 的 GND 和驱动器电源 GND 共地了吗？不共地 = 悬空乱电平 = 不动。
 *   3. PA2 接的是该驱动器的 RXD 吗？
 *   4. 驱动器地址拨码 = MOTOR_ADDR 吗？不转就依次改成 1/2/3/4 重编一次试。
 * ========================================================================= */

#define MOTOR_ADDR        1U        /* 要试的电机地址：1 / 2 / 3 / 4 */
#define PULSES_PER_REV    3200UL    /* 1.8° 电机 16 细分；32 细分改 6400 */
#define HALF_REV_PULSES   (PULSES_PER_REV / 2UL)   /* 每次走半圈，撞不到头 */
#define FD_ACCEL          1U        /* 位置模式加速度：0/1 = 直接启动 */
#define MOVE_WAIT_BASE_MS 1500UL    /* 走半圈的基准等待时间（按转速放大） */
#define DWELL_MS          2500UL    /* 走完停多久（看得很清楚） */
#define FRAME_GAP_MS      10UL
#define BOOT_BLINK_MS     250UL

typedef struct { uint16_t rpm; uint8_t accel; } DiagCfg;

/* 逐档试速度/加速度，从"最容易转起来"的档开始 */
static const DiagCfg diag_tune[] = {
    {  60U, 1U },
    {  30U, 1U },
    { 120U, 1U },
    {  60U, 0U },
    { 300U, 1U }
};

#define DIAG_COUNT ((uint8_t)(sizeof(diag_tune) / sizeof(diag_tune[0])))

static void led_blink(uint8_t times)
{
    uint8_t i;

    for (i = 0U; i < times; i++)
    {
        LED1_ON;
        Delay_ms(BOOT_BLINK_MS);
        LED1_OFF;
        Delay_ms(BOOT_BLINK_MS);
    }
}

int main(void)
{
    uint8_t  i;
    uint16_t rpm;
    uint8_t  accel;
    int32_t  pulses;
    uint32_t wait_ms;

    Delay_Init();
    LED_Init();
    Key_Init();
    Stepper_Init();

    /* 上电闪 2 下：和 main.c 的"闪 1 下"区分开，确认烧进去的是诊断固件 */
    led_blink(2U);

    (void)Stepper_ResetStallAt(MOTOR_ADDR);     /* 0E 52：先解堵转保护 */
    Delay_ms(FRAME_GAP_MS);
    (void)Stepper_StopAt(MOTOR_ADDR);           /* FE 停车 */
    Delay_ms(FRAME_GAP_MS);
    (void)Stepper_EnableAt(MOTOR_ADDR, 1U);     /* F3 使能 */
    Delay_ms(500UL);

    while (1)
    {
        for (i = 0U; i < DIAG_COUNT; i++)
        {
            rpm    = diag_tune[i].rpm;
            accel  = diag_tune[i].accel;
            pulses = (int32_t)HALF_REV_PULSES;
            /* 按转速放大等待时间：60RPM 走半圈约 0.5s，30RPM 约 1s */
            wait_ms = (uint32_t)((60000UL / (uint32_t)rpm) + MOVE_WAIT_BASE_MS);

            LED1_ON;
            /* FD 位置模式：相对运动，只发一次，驱动器自己走完 */
            (void)Stepper_MoveRelativeAt2(MOTOR_ADDR, STEPPER_DIR_POSITIVE,
                                          pulses, rpm, accel, wait_ms);
            (void)Stepper_StopAt(MOTOR_ADDR);
            LED1_OFF;

            led_blink(2U);                      /* 走完一次闪 2 下 */
            Delay_ms(DWELL_MS);                 /* 停一会儿，方便看清"动了没有" */

            /* 换下一档前清一次堵转保护，避免上一档堵转后一直不转 */
            (void)Stepper_ResetStallAt(MOTOR_ADDR);
            Delay_ms(FRAME_GAP_MS);
        }
    }
}
