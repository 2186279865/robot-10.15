/* gripper_inline.h - 抓夹模块的实现（供本工程内联编译用）
 *
 * 为什么实现放在头文件里：
 *   Keil 一直开着本工程，它内存里的文件列表是旧的，外部改 .uvprojx 会被它保存时
 *   覆盖（已出现两次），所以新加的 .c 文件无法参与编译。把实现放进已在包含路径
 *   （..\Drive\Include）里的头文件，由 main.c 编译时带入，就不需要动工程文件。
 *
 * 接口声明见 gripper.h，本文件只提供实现。
 * 以后若把 Drive\Source\gripper.c 正式加进 Keil 工程，请删掉 main.c 里的
 * #include "gripper_inline.h"，避免函数重复定义。
 */

#include "gripper.h"
#include "servo.h"
#include "delay.h"

static uint16_t gripper_deg;            /* 当前夹爪角度 */

static void gripper_apply(void)
{
    Servo_SetAngle(gripper_deg);
    Delay_ms(GRIPPER_STEP_WAIT_MS);     /* 给舵机走位时间，再允许下一次动作 */
}

void Gripper_Init(void)
{
    gripper_deg = GRIPPER_OPEN_DEG;     /* 上电停在松开位置 */
    Servo_SetAngle(gripper_deg);
    Delay_ms(GRIPPER_STEP_WAIT_MS);
}

uint8_t Gripper_Grab(void)
{
    if (gripper_deg <= GRIPPER_GRAB_DEG)
    {
        return 0U;                      /* 已经夹紧，不再动作 */
    }

    if ((uint16_t)(gripper_deg - GRIPPER_GRAB_DEG) >= GRIPPER_STEP_DEG)
    {
        gripper_deg = (uint16_t)(gripper_deg - GRIPPER_STEP_DEG);
    }
    else
    {
        gripper_deg = GRIPPER_GRAB_DEG; /* 最后一步不足一个步长就精确到位 */
    }

    gripper_apply();
    return 1U;
}

uint8_t Gripper_Release(void)
{
    if (gripper_deg >= GRIPPER_OPEN_DEG)
    {
        return 0U;                      /* 已经松开，不再动作 */
    }

    if ((uint16_t)(GRIPPER_OPEN_DEG - gripper_deg) >= GRIPPER_STEP_DEG)
    {
        gripper_deg = (uint16_t)(gripper_deg + GRIPPER_STEP_DEG);
    }
    else
    {
        gripper_deg = GRIPPER_OPEN_DEG;
    }

    gripper_apply();
    return 1U;
}

void Gripper_GrabNow(void)
{
    gripper_deg = GRIPPER_GRAB_DEG;
    gripper_apply();
}

void Gripper_ReleaseNow(void)
{
    gripper_deg = GRIPPER_OPEN_DEG;
    gripper_apply();
}

void Gripper_ReadyNow(void)
{
    gripper_deg = GRIPPER_READY_DEG;
    gripper_apply();
}

uint16_t Gripper_GetAngle(void)
{
    return gripper_deg;
}

uint8_t Gripper_IsGrabbed(void)
{
    return (uint8_t)(gripper_deg <= GRIPPER_GRAB_DEG);
}

uint8_t Gripper_IsOpen(void)
{
    return (uint8_t)(gripper_deg >= GRIPPER_OPEN_DEG);
}
