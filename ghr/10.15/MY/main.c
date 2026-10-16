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

/* 上位机通信层：
 *   host_transport.h      传输层接口（实现是 Drive/Source/host_usb.c，独立编译单元）
 *   control_inline.h      统一控制状态 + 非阻塞下发
 *   host_protocol_inline.h 行协议（分帧/校验/命令分发/应答/事件/遥测）
 * 三者都靠 Stepper_Millis() 的毫秒时基，主循环里不许出现阻塞等待。 */
#include "host_transport.h"
#include "control_inline.h"
#include "host_protocol_inline.h"

/* ================= PA0 按键 + PC 上位机 共用一套状态 =================
 * 轴地址：Z=1、托盘=3、X=4、Y=5（见 stepper.h 的 STEPPER_ADDR_*）。
 * PA0 只管 Z 轴（地址 1），PC 端可以管四轴和夹爪。
 *
 *   上电：闪 1 下（程序活着）-> 0E 52 解堵转 -> FE 停车 -> F3 使能 Z -> 等 500ms
 *   PA0（空闲）：Z 轴按 speed_rpm / acc 参数连续转，LED 常亮
 *   PA0（有轴在转）：停止全部并置停止锁存，LED 回到空闲心跳
 *   PA0（已锁存且都停着）：解除锁存，再按一次才启动（人工就地复位；
 *                           PC 端等价的命令是 stop_clear）
 *
 * 【和上一版的区别】
 *   1. 去掉"自动换档"阶梯：那是只发不收、靠换参数试出来的权宜做法。
 *      上位机接上后由 PC 指定转速/加速度，固件不再自作主张换档。
 *   2. 去掉每 250ms 补发 F6：改由统一控制状态记账，重复下发只会在
 *      总线上制造冲突（帧间至少要留 frame_gap_ms）。
 *   3. 时间基准从 "ms += KEY_TASK_MS" 估算改成 Stepper_Millis() 实测。
 *   4. 下发动作全部走 control_apply_*，PA0 与 PC 看到的是同一份状态。
 *
 * 【回读】
 *   回读：PA3 接驱动器 TXD。哪几个轴真接了回读线由参数 readback_mask 声明，
 *   hello 里的 readback 列表同样取这个参数，界面不会显示任何"实测"数据。
 *
 * 如果 PA0 还是不动，按顺序排查（前两条是硬件，代码解决不了）：
 *   1. 驱动器有没有接 12~48V 电机电源？只靠 STM32 的 USB/5V 供电电机不动。
 *   2. STM32 的 GND 和驱动器电源的 GND 必须接在一起（共地）。
 *   3. 看 LED1（PC13）：上电闪 1 下、空闲时每 1.2 秒闪一下 = 程序在跑。
 *   4. 确认 PA2 接的是 1 号驱动器的 RXD；该驱动器地址拨码 = 1。
 * ========================================================================= */

#define MOTOR_ADDR        1U        /* PA0 控制的轴：Z = 1 */
#define FRAME_GAP_MS      10UL      /* 连续两帧之间的间隔 */
#define BOOT_BLINK_MS     250UL
#define IDLE_BLINK_ON_MS  60UL
#define IDLE_BLINK_MS     1200UL

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

/* 有没有轴正在速度/位置模式运行（PA0 用它决定"启动"还是"停止"） */
static uint8_t motor_running(void)
{
    uint8_t i;

    for (i = 0U; i < CONTROL_AXIS_COUNT; i++)
    {
        if (control_axis[i].mode != CONTROL_MODE_STOP)
        {
            return 1U;
        }
    }
    return 0U;
}

int main(void)
{
    uint32_t ms;
    uint32_t blink_last;
    uint32_t task;

    Delay_Init();
    LED_Init();
    Key_Init();
    Stepper_Init();

    /* USB CDC：只做板级初始化，不等枚举、不阻塞 */
    HostLink_Init();
    host_protocol_init();
    control_gripper_init();   /* PB6/TIM4 舵机 PWM 上电起振，停在松开位 */

    /* 上电自检：闪 1 下 = 初始化完成、程序已经跑到主循环 */
    led_blink(1U);

    /* 照例36 的上电顺序：停车 -> 使能 -> 等 500ms，之后驱动器才认速度指令 */
    (void)Stepper_ResetStallAt(MOTOR_ADDR);     /* 0E 52：清一次堵转保护 */
    Delay_ms(FRAME_GAP_MS);
    (void)control_apply_stop(MOTOR_ADDR);
    Delay_ms(FRAME_GAP_MS);
    (void)control_apply_enable(MOTOR_ADDR, 1U);
    Delay_ms(500UL);

    LED1_OFF;
    ms = Stepper_Millis();
    blink_last = ms;

    while (1)
    {
        ms = Stepper_Millis();

        /* USB 事件轮询（枚举/复位/拔线）+ 协议任务（收字节->分帧->分发->应答） */
        HostLink_Poll();
        host_protocol_task();

        /* ------------- 接按键（1ms 轮询，内部 20ms 消抖，长按不连发） ------------- */
        if (Key_Poll(ms))
        {
            if (control_stop_latched() && !motor_running())
            {
                /* 已锁存且都停着：人工就地解除锁存，下一次按键才启动 */
                control_clear_stop();
                blink_last = ms;
            }
            else if (motor_running())
            {
                /* 有轴在转：PA0 = 停止全部（置锁存，由 control_service 错发停车帧） */
                control_latch_stop(ms);
                LED1_OFF;
                blink_last = ms;
            }
            else if (!control_stop_latched())
            {
                /* 空闲：Z 轴按参数转起来 */
                task = 0U;
                (void)control_apply_speed(MOTOR_ADDR, STEPPER_DIR_POSITIVE,
                                          host_param_value("speed_rpm"),
                                          (uint8_t)host_param_value("acc"),
                                          &task);
                LED1_ON;                         /* 常亮 = 正在转 */
            }
            else
            {
                /* 其他情况（锁存中但仍有轴报运行）：保持锁存，不启动 */
            }
        }

        if (motor_running())
        {
            LED1_ON;
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
    }
}
