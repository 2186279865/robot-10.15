#include "gripper.h"
#include "servo.h"
#include "delay.h"

/* 抓夹（舵机夹爪）模块实现。
 *
 * 角度模型：current_deg 就是直接送给舵机的物理角度。
 *     GRIPPER_OPEN_DEG(180) --逐步收紧--> GRIPPER_GRAB_DEG(135)
 * 每次动作改变 GRIPPER_STEP_DEG(45) 度，并在最后一步夹紧 / 松开时精确落在端点，
 * 不会因为步长不整除而走不到位。
 */

static uint16_t current_deg;    /* 当前夹爪角度 */

/* 夹紧一步：角度朝 GRIPPER_GRAB_DEG 方向走一步，并夹在端点内 */
static void step_toward_grab(void)
{
    if (current_deg > GRIPPER_GRAB_DEG)
    {
        if ((uint16_t)(current_deg - GRIPPER_GRAB_DEG) >= GRIPPER_STEP_DEG)
        {
            current_deg = (uint16_t)(current_deg - GRIPPER_STEP_DEG);
        }
        else
        {
            current_deg = GRIPPER_GRAB_DEG;     /* 最后一步不足一个步长就精确到位 */
        }
    }
}

/* 松开一步：角度朝 GRIPPER_OPEN_DEG 方向走一步，并夹在端点内 */
static void step_toward_open(void)
{
    if (current_deg < GRIPPER_OPEN_DEG)
    {
        if ((uint16_t)(GRIPPER_OPEN_DEG - current_deg) >= GRIPPER_STEP_DEG)
        {
            current_deg = (uint16_t)(current_deg + GRIPPER_STEP_DEG);
        }
        else
        {
            current_deg = GRIPPER_OPEN_DEG;
        }
    }
}

static void apply(void)
{
    Servo_SetAngle(current_deg);
    Delay_ms(GRIPPER_STEP_WAIT_MS);     /* 给舵机走位时间，再允许下一次动作 */
}

void Gripper_Init(void)
{
    current_deg = GRIPPER_OPEN_DEG;     /* 上电停在松开位置 */
    Servo_SetAngle(current_deg);
    Delay_ms(GRIPPER_STEP_WAIT_MS);
}

uint8_t Gripper_Grab(void)
{
    if (current_deg <= GRIPPER_GRAB_DEG)
    {
        return 0U;                      /* 已经夹紧，不再动作 */
    }

    step_toward_grab();
    apply();
    return 1U;
}

uint8_t Gripper_Release(void)
{
    if (current_deg >= GRIPPER_OPEN_DEG)
    {
        return 0U;                      /* 已经松开，不再动作 */
    }

    step_toward_open();
    apply();
    return 1U;
}

void Gripper_GrabNow(void)
{
    current_deg = GRIPPER_GRAB_DEG;
    apply();
}

void Gripper_ReleaseNow(void)
{
    current_deg = GRIPPER_OPEN_DEG;
    apply();
}

uint16_t Gripper_GetAngle(void)
{
    return current_deg;
}

uint8_t Gripper_IsGrabbed(void)
{
    return (uint8_t)(current_deg <= GRIPPER_GRAB_DEG);
}

uint8_t Gripper_IsOpen(void)
{
    return (uint8_t)(current_deg >= GRIPPER_OPEN_DEG);
}
