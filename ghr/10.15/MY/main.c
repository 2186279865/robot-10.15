#include "stm32f4xx.h"
#include "led.h"
#include "delay.h"
#include "gripper.h"
#include "key.h"
#include "stepper.h"

/* 模块实现（本工程不把单独的 .c 加进 Keil 工程，所以在这里内联进来）。
 * 以后如果用 Keil 的 Add Existing Files 把 gripper.c / key.c / stepper.c
 * 正式加进工程，把下面这几行删掉即可，接口不用改。 */
#include "gripper_inline.h"
#include "key_inline.h"
#include "stepper_inline.h"

/* ================= PA0 按键 -> 1 号电机 转 / 停（toggle） =================
 * 只操作 1 号电机（地址 01）。按一下开始连续转，再按一下停止；可以反复按。
 *
 *   上电：闪 1 下（程序活着）-> 0E 52 解堵转 -> FE 停车 -> F3 使能 -> 等 500ms
 *   按 PA0 第 1 次：发 F6 速度模式，1 号电机持续转，LED 常亮
 *   按 PA0 第 2 次：发 FE 立即停止，LED 灭
 *   再按：继续转 / 再停，如此反复
 *
 * 【本版和上一版的区别：起转参数】
 *   上一版用 RPM=300、acc=128。加速度档位越大，驱动器的加速斜坡越缓、
 *   起转瞬间的力矩越小；300RPM 对小电机也偏快，起转容易触发堵转保护 ->
 *   一动不动。本版改成 acc=1（近乎直接启动，起转最猛）+ RPM=60（先慢，
 *   确保能转起来），并把"转速+加速度"做成阶梯：第一次按不动就自动换下一档
 *   重发，至少能自己找到一组能转的参数。RUN_STARTUP_TUNE=0 可关掉自动换档。
 *
 * 说明：本工程只发不收，PA2 单线接到 1 号驱动器的 RXD；驱动器 TXD 不用接。
 * 上电会先发 FE 停车，机械负载如果停在行程中间要注意先手动摆到安全位置。
 *
 * 如果还是不动，按顺序排查（前两条是硬件，代码解决不了）：
 *   1. 驱动器有没有接 12~48V 电机电源？只靠 STM32 的 USB/5V 供电电机不动。
 *   2. STM32 的 GND 和驱动器电源的 GND 必须接在一起（共地）。不共地时驱动器
 *      收到的是悬空乱电平，表现就是"程序在跑但电机纹丝不动"。
 *   3. 看 LED1（PC13）：上电闪 1 下、空闲时每 1.2 秒闪一下 = 程序在跑；
 *      按键后常亮 = 按键已响应，此时不动就是驱动器那边的事。
 *   4. 确认 PA2 接的是 1 号驱动器的 RXD；确认该驱动器地址拨码 = 1（帧头 [01]）。
 *      所有驱动器挂同一条 PA2 总线上，靠地址区分。
 *   5. 用同目录的 main_diag.c（位置模式走 1 圈）单独验证硬件通路：
 *      能转 = 硬件没问题，问题在速度模式参数或按键；不能转 = 硬件/接线问题。
 * ========================================================================= */

#define MOTOR_ADDR        1U        /* 1 号电机；与例36 的 Z_ADDRESS 一致 */

#define MOTOR_RESEND_MS   250UL     /* 运行中补发 F6 的间隔，防首帧丢失 */
#define FRAME_GAP_MS      10UL      /* 连续两帧之间的间隔，避免驱动器处理冲突 */

#define BOOT_BLINK_MS     250UL     /* 上电闪灯 / 按键反馈的亮灭时长 */
#define IDLE_BLINK_ON_MS  60UL      /* 空闲心跳：亮多久 */
#define IDLE_BLINK_MS     1200UL    /* 空闲心跳：一个周期 */

/* 起转参数阶梯：转速 RPM、加速度档位。按不动就换下一档。 */
#define RUN_STARTUP_TUNE  1U        /* 1 = 允许自动换档重试；0 = 只用第 1 档 */
#define TUNE_RETRY_MS     1500UL    /* 每一档试多久后换下一档 */

typedef struct { uint16_t rpm; uint8_t accel; } MotorRunCfg;

static const MotorRunCfg motor_tune[] = {
    {  60U,   1U },     /* 第 1 档：慢速 + 近乎直接启动，起转最猛 */
    {  60U,   0U },     /* 第 2 档：完全直接启动 */
    { 120U,   1U },     /* 第 3 档 */
    { 300U,   1U },     /* 第 4 档：和旧版转速一致 */
    {  30U,   1U }      /* 第 5 档：最慢 */
};

#define TUNE_COUNT ((uint8_t)(sizeof(motor_tune) / sizeof(motor_tune[0])))

/* ---------------- 小工具 ---------------- */

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

static void motor_enable(uint8_t addr, uint8_t on)
{
    (void)Stepper_EnableAt(addr, on);
}

static void motor_speed(uint8_t addr, uint8_t dir, uint16_t rpm, uint8_t accel)
{
    (void)Stepper_RunSpeedAt2(addr, dir, rpm, accel);   /* F6：持续转，不自动停 */
}

static void motor_stop(uint8_t addr)
{
    /* 连发两帧，避免单帧丢失后电机停不下来 */
    (void)Stepper_StopAt(addr);
    Delay_ms(FRAME_GAP_MS);
    (void)Stepper_StopAt(addr);
}

int main(void)
{
    uint8_t  motor_state = 0U;      /* 0 = 停，1 = 转 */
    uint8_t  tune_index  = 0U;      /* 当前起转参数档位 */
    uint32_t ms          = 0U;
    uint32_t last_resend = 0U;
    uint32_t last_tune   = 0U;
    uint32_t blink_last  = 0U;

    Delay_Init();
    LED_Init();
    Key_Init();
    Stepper_Init();

    /* 上电自检：闪 1 下 = 初始化完成、程序已经跑到主循环 */
    led_blink(1U);

    /* 照例36 的上电顺序：停车 -> 使能 -> 等 500ms，之后驱动器才认速度指令 */
    (void)Stepper_ResetStallAt(MOTOR_ADDR);     /* 0E 52：清一次堵转保护 */
    Delay_ms(FRAME_GAP_MS);
    motor_stop(MOTOR_ADDR);
    Delay_ms(FRAME_GAP_MS);
    motor_enable(MOTOR_ADDR, 1U);
    Delay_ms(500UL);

    LED1_OFF;
    blink_last = ms;

    while (1)
    {
        /* ------------- 接按键（1ms 轮询，内部 20ms 消抖，长按不连发） ------------- */
        if (Key_Poll(ms))
        {
            if (motor_state == 0U)
            {
                /* 按一下：1 号电机开始连续转（固定一个方向，便于观察） */
                motor_state = 1U;
                tune_index  = 0U;
                last_resend = ms;
                last_tune   = ms;
                blink_last  = ms;
                motor_speed(MOTOR_ADDR, STEPPER_DIR_POSITIVE,
                            motor_tune[0].rpm, motor_tune[0].accel);
                LED1_ON;                                 /* 常亮 = 正在转 */
            }
            else
            {
                /* 再按一下：停止转动 */
                motor_state = 0U;
                motor_stop(MOTOR_ADDR);
                LED1_OFF;
                blink_last = ms;
            }
        }

        if (motor_state != 0U)
        {
            /* 运行中：每隔 MOTOR_RESEND_MS 补发一帧 F6，首帧丢了也能转起来 */
            if ((uint32_t)(ms - last_resend) >= MOTOR_RESEND_MS)
            {
                last_resend = ms;
                motor_speed(MOTOR_ADDR, STEPPER_DIR_POSITIVE,
                            motor_tune[tune_index].rpm, motor_tune[tune_index].accel);
            }

#if RUN_STARTUP_TUNE
            /* 只发不收，听不到驱动器回话，所以用"换参数"代替判断：
             * 每一档跑满 TUNE_RETRY_MS 就换下一档再发；到最后一档就反复用它。
             * 电机开始转 = 那一档可用；一直不转 = 硬件/接线问题。 */
            if ((uint32_t)(ms - last_tune) >= TUNE_RETRY_MS)
            {
                last_tune = ms;

                if ((uint8_t)(tune_index + 1U) < TUNE_COUNT)
                {
                    tune_index++;
                    last_resend = ms;
                    motor_speed(MOTOR_ADDR, STEPPER_DIR_POSITIVE,
                                motor_tune[tune_index].rpm, motor_tune[tune_index].accel);
                }
            }
#endif
        }
        else
        {
            /* 空闲：慢闪一下，说明程序还活着、按键正在被轮询 */
            uint32_t t = (uint32_t)(ms - blink_last);

            if (t < IDLE_BLINK_ON_MS)
            {
                LED1_ON;
            }
            else if (t >= IDLE_BLINK_MS)
            {
                LED1_OFF;
                blink_last = ms;
            }
            else
            {
                LED1_OFF;
            }
        }

        Key_TaskDelay();            /* 1ms 节奏，Key_Poll 的消抖靠这个步进 */
        ms += KEY_TASK_MS;
    }
}
