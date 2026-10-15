#include "stm32f4xx.h"
#include "led.h"
#include "delay.h"
#include "servo.h"
#include "gripper.h"
#include "key.h"
#include "stepper.h"

/* 模块实现（本工程不把单独的 .c 加进 Keil 工程，所以在这里内联进来）。
 * 以后如果用 Keil 的 Add Existing Files 把 gripper.c / key.c / stepper.c
 * 正式加进工程，把下面这几行删掉即可，接口不用改。 */
#include "gripper_inline.h"
#include "key_inline.h"
#include "stepper_inline.h"

/* ===================== 步进电机 + 抓夹（按键版） =====================
 * 上电顺序照例36：FE 停车 -> F3 使能 -> 等 500ms。
 * 按 PA0 一下：全部电机（地址 1~4）按速度模式转 NUDGE_MS 毫秒后自动停车，
 *              同时抓夹收紧一步 45 度。
 *
 * 如果电机还是不动，看 LED 的开机闪烁次数（串口回环自检结果）：
 *      闪 1 次 = PA2↔PA3 短接时回显正常
 *      闪 2 次 = PA2 上有外部接线但没回显
 *      闪 3 次 = PA2 上什么都没接  <- 驱动器信号线没接到 PA2 时就是这个
 * ================================================================== */

#define NUDGE_RUN_MS   1000UL   /* 每次按键电机转动时长 */
#define NUDGE_RPM      STEPPER_SPEED_RPM

static void led_blink(uint8_t times)
{
    uint8_t i;
    for (i = 0U; i < times; i++)
    {
        LED1_ON;
        Delay_ms(250);
        LED1_OFF;
        Delay_ms(250);
    }
    Delay_ms(700);
}

int main(void)
{
    uint32_t ms = 0U;
    uint8_t  loop;
    uint8_t  step_dir = STEPPER_DIR_POSITIVE;    /* 每次按键方向交替 */

    Delay_Init();
    LED_Init();
    Key_Init();
    Servo_Init();
    Gripper_Init();
    Stepper_Init();

    /* 开机：串口回环自检结果用 LED 报告 */
    loop = Stepper_LoopbackTest();
    if (loop == 1U)      { led_blink(1U); }
    else if (loop == 2U) { led_blink(2U); }
    else if (loop == 0U) { led_blink(3U); }
    else                 { led_blink(5U); }

    /* 照例36 的顺序：全部停车 -> 全部使能 -> 等 500ms */
    (void)Stepper_StopAll();
    (void)Stepper_EnableAll(1U);
    Delay_ms(500UL);

    LED1_OFF;

    while (1)
    {
        if (Key_Poll(ms))
        {
            if (Gripper_Grab())
            {
                LED1_ON;
            }
            else
            {
                LED1_OFF;
            }

            /* 全部电机走一段，然后自动停车 */
            (void)Stepper_NudgeAll(step_dir, NUDGE_RPM, NUDGE_RUN_MS);
            step_dir = (uint8_t)(1U - step_dir);
        }

        Key_TaskDelay();
        ms += KEY_TASK_MS;
    }
}
